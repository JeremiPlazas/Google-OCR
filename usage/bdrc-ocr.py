import io
import os
import hashlib
from pathlib import Path

import boto3
import botocore
from PIL import Image
import requests
import rdflib
from rdflib import URIRef, Literal
from rdflib.namespace import Namespace, NamespaceManager


ARCHIVE_BUCKET = "archive.tbrc.org"
OCR_OUTPUT_BUCKET = "ocr.bdrc.io"

os.environ['AWS_SHARED_CREDENTIALS_FILE'] = "~/.aws/credentials"

BDR = Namespace("http://purl.bdrc.io/resource/")
NSM = NamespaceManager(rdflib.Graph())
NSM.bind("bdr", BDR)


def get_s3_bucket(bucket_name):
	s3 = boto3.resource('s3')
	return s3.Bucket(bucket_name)


def get_value(json_node):
	if json_node['type'] == 'literal':
		return json_node['value']
	else:
		return NSM.qname(URIRef(json_node["value"]))


def get_s3_image_list(volume_prefix_url):
	"""
	returns the content of the dimension.json file for a volume ID, accessible at:
	https://iiifpres.bdrc.io/il/v:bdr:V22084_I0888 for volume ID bdr:V22084_I0888
	"""
	r = requests.get(f'https://iiifpres.bdrc.io/il/v:{volume_prefix_url}')
	if r.status_code != 200:
		print("error "+r.status_code+" when fetching volumes for "+qname)
		return
	return r.json()


def get_volume_infos(work_prefix_url):
	"""
	the input is something like bdr:W22084, the output is a list like:
	[ 
	  {
		"vol_num": 1,
		"volume_prefix_url": "bdr:V22084_I0886",
		"imagegroup": "I0886"
	  },
	  ...
	]
	"""
	r = requests.get(f'http://purl.bdrc.io/query/table/volumesForWork?R_RES={work_prefix_url}&format=json&pageSize=400')
	if r.status_code != 200:
		print("error %d when fetching volumes for %s" %(r.status_code, qname))
		return
	# the result of the query is already in ascending volume order
	res = r.json()
	for b in res["results"]["bindings"]:
		volume_prefix_url = NSM.qname(URIRef(b["volid"]["value"]))
		yield {
			"vol_num": get_value(b["volnum"]), 
			"volume_prefix_url": get_value(b["volid"]),
			"imagegroup": get_value(b["imggroup"])
			}


def get_s3_prefix_path(work_local_id, imagegroup):
	"""
	the input is like W22084, I0886. The output is an s3 prefix ("folder"), the function
	can be inspired from 
	https://github.com/buda-base/volume-manifest-tool/blob/f8b495d908b8de66ef78665f1375f9fed13f6b9c/manifestforwork.py#L94
	which is documented
	"""
	md5 = hashlib.md5(str.encode(work_local_id))
	two = md5.hexdigest()[:2]

	pre, rest = imagegroup[0], imagegroup[1:]
	if pre == 'I' and rest.isdigit() and len(rest) == 4:
		suffix = rest
	else:
		suffix = imagegroup
	
	return f'Works/{two}/{work_local_id}/images/{work_local_id}-{suffix}'

def get_s3_bits(s3path, bucket):
	"""
	get the s3 binary data in memory
	"""
	f = io.BytesIO()
	try:
		bucket.download_fileobj(s3path, f)
		return f
	except botocore.exceptions.ClientError as e:
		if e.response['Error']['Code'] == '404':
			print('The object does not exist.')
		else:
			raise
	return

def save_file(bits, origfilename, imagegroup_output_dir):
	"""
	uses pillow to interpret the bits as an image and save as a format
	that is appropriate for Google Vision (png instead of tiff for instance).
	This may also apply some automatic treatment
	"""
	imagegroup_output_dir.mkdir(exist_ok=True, parents=True)
	output_fn = imagegroup_output_dir/origfilename
	if origfilename.endswith('.tif'):
		img = Image.open(bits)
		output_fn.write_bytes(img)
	else:
		output_fn.write_bytes(bits.getbuffer())

def save_images_for_vol(volume_prefix_url, work_local_id, imagegroup, images_base_dir, bucket):
	"""
	this function gets the list of images of a volume and download all the images from s3.
	The output directory is output_base_dir/work_local_id/imagegroup
	"""
	imagelist = get_s3_image_list(volume_prefix_url)
	s3prefix = get_s3_prefix_path(work_local_id, imagegroup)
	for imageinfo in imagelist:
		s3path = s3prefix+"/"+imageinfo['filename']
		filebits = get_s3_bits(s3path, bucket)
		imagegroup_output_dir = images_base_dir/work_local_id/imagegroup
		save_file(filebits, imageinfo['filename'], imagegroup_output_dir)

def apply_ocr_on_folder(imagesfolder, work_local_id, imagegroup, ocr_base_dir):
	"""
	This function goes through all the images of imagesfolder, passes them to the Google Vision API
	and saves the output files to ocr_base_dir/work_local_id/imagegroup/filename.json.gz
	"""
	return

def get_info_json():
	"""
	This returns an object that can be serialied as info.json as specified for BDRC s3 storage.
	"""
	return

def archive_on_s3(images_base_dir, ocr_base_dir, work_local_id, imagegroup):
	"""
	This function uploads the images on s3, according to the schema set up by BDRC, see documentation
	"""
	return


if __name__ == "__main__":
	archive_bucket = get_s3_bucket(ARCHIVE_BUCKET)
	ocr_output_bucket = get_s3_bucket(OCR_OUTPUT_BUCKET)

	work = 'bdr:W4CZ5369'
	for vol_info in get_volume_infos(work):
		work_local_id = work.split(':')[-1] if ':' in work else work
		save_images_for_vol(
			volume_prefix_url=vol_info['volume_prefix_url'],
			work_local_id=work_local_id, 
			imagegroup=vol_info['imagegroup'],
			images_base_dir=Path('./'),
			bucket=archive_bucket
		)