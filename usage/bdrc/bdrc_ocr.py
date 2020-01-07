from datetime import datetime
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import pytz
import shutil

import boto3
import botocore
from PIL import Image
import requests
import rdflib
from rdflib import URIRef, Literal
from rdflib.namespace import Namespace, NamespaceManager

from ocr.google_ocr import get_text_from_image


# S3 config
os.environ['AWS_SHARED_CREDENTIALS_FILE'] = "~/.aws/credentials"
ARCHIVE_BUCKET = "archive.tbrc.org"
OCR_OUTPUT_BUCKET = "ocr.bdrc.io"
S3 = boto3.resource('s3') 
archive_bucket = S3.Bucket(ARCHIVE_BUCKET)
ocr_output_bucket = S3.Bucket(OCR_OUTPUT_BUCKET)

# URI config
BDR = Namespace("http://purl.bdrc.io/resource/")
NSM = NamespaceManager(rdflib.Graph())
NSM.bind("bdr", BDR)

# s3 bucket directory config
SERVICE = "vision"
BATCH_PREFIX = 'batch'
IMAGES = 'images'
OUTPUT = 'output'
INFO_FN = 'info.json'

# local directory config
DATA_PATH = Path('./archive')
IMAGES_BASE_DIR = DATA_PATH/IMAGES
OCR_BASE_DIR = DATA_PATH/OUTPUT


def is_archive_exist(key):
	try:
		s3.Object('my-bucket', 'dootdoot.jpg').load()
	except botocore.exceptions.ClientError as e:
		if e.response['Error']['Code'] == "404":
			return False
	else:
		return True


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
		print("error %d when fetching volumes for %s" %(r.status_code, work_prefix_url))
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


def get_s3_prefix_path(work_local_id, imagegroup, service=None, batch_prefix=None, data_types=None):
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
	
	base_dir = f'Works/{two}/{work_local_id}'
	if service:
		batch_dir = f'{base_dir}/{service}/{batch_prefix}001'
		paths = {BATCH_PREFIX: batch_dir}
		for dt in data_types:
			paths[dt] = f'{batch_dir}/{dt}/{work_local_id}-{suffix}'
		return paths
	return f'{base_dir}/images/{work_local_id}-{suffix}'


def get_s3_bits(s3path):
	"""
	get the s3 binary data in memory
	"""
	f = io.BytesIO()
	try:
		archive_bucket.download_fileobj(s3path, f)
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
	if origfilename.endswith('.tif'):
		img = Image.open(bits)
		output_fn = imagegroup_output_dir/f'{origfilename.split(".")[0]}.png'
		if output_fn.is_file(): return
		img.save(str(output_fn))
	else:
		output_fn = imagegroup_output_dir/origfilename
		if output_fn.is_file(): return
		output_fn.write_bytes(bits.getbuffer())


def save_images_for_vol(volume_prefix_url, work_local_id, imagegroup, images_base_dir):
	"""
	this function gets the list of images of a volume and download all the images from s3.
	The output directory is output_base_dir/work_local_id/imagegroup
	"""
	s3prefix = get_s3_prefix_path(work_local_id, imagegroup)
	for imageinfo in get_s3_image_list(volume_prefix_url):
		s3path = s3prefix+"/"+imageinfo['filename']
		filebits = get_s3_bits(s3path)
		imagegroup_output_dir = images_base_dir/work_local_id/imagegroup
		save_file(filebits, imageinfo['filename'], imagegroup_output_dir)


def gzip_str(string_):
	# taken from https://gist.github.com/Garrett-R/dc6f08fc1eab63f94d2cbb89cb61c33d
	out = io.BytesIO()

	with gzip.GzipFile(fileobj=out, mode='w') as fo:
		fo.write(string_.encode())

	bytes_obj = out.getvalue()
	return bytes_obj


def apply_ocr_on_folder(images_base_dir, work_local_id, imagegroup, ocr_base_dir):
	"""
	This function goes through all the images of imagesfolder, passes them to the Google Vision API
	and saves the output files to ocr_base_dir/work_local_id/imagegroup/filename.json.gz
	"""
	images_dir = images_base_dir/work_local_id/imagegroup
	ocr_output_dir = ocr_base_dir/work_local_id/imagegroup
	ocr_output_dir.mkdir(exist_ok=True, parents=True)

	for img_fn in images_dir.iterdir():
		result_fn = ocr_output_dir/f'{img_fn.stem}.json.gz'
		if result_fn.is_file(): continue
		result = get_text_from_image(str(img_fn))
		gzip_result = gzip_str(result)
		result_fn.write_bytes(gzip_result)


def get_info_json():
	"""
	This returns an object that can be serialied as info.json as specified for BDRC s3 storage.
	"""
	# get current date and time
	now = datetime.now(pytz.utc).isoformat()

	info = {
		"timestamp": now.split('.')[0],
		'imagesfolder': IMAGES
	}
	
	return info
	

def archive_on_s3(images_base_dir, ocr_base_dir, work_local_id, imagegroup, s3_paths):
	"""
	This function uploads the images on s3, according to the schema set up by BDRC, see documentation
	"""
	# save info json
	info_json = get_info_json()
	s3_ocr_info_path = f'{s3_paths[BATCH_PREFIX]}/{INFO_FN}'
	ocr_output_bucket.put_object(
		Key=s3_ocr_info_path,
		Body=(bytes(json.dumps(info_json).encode('UTF-8')))
	)
	
	# archive images
	images_dir = images_base_dir/work_local_id/imagegroup
	for img_fn in images_dir.iterdir():
		s3_image_path = f'{s3_paths[IMAGES]}/{img_fn.name}'
		ocr_output_bucket.put_object(Key=s3_image_path, Body=img_fn.read_bytes())
	
	# archive ocr output
	ocr_output_dir = ocr_base_dir/work_local_id/imagegroup
	for out_fn in ocr_output_dir.iterdir():
		s3_output_path = f'{s3_paths[OUTPUT]}/{out_fn.name}'
		ocr_output_bucket.put_object(Key=s3_output_path, Body=out_fn.read_bytes())


def clean_up(data_path, work_local_id, imagegroup):
	"""
	delete all the images and output of the archived volume (imagegroup)
	"""
	vol_image_path = data_path/IMAGES/work_local_id/imagegroup
	vol_output_path = data_path/OUTPUT/work_local_id/imagegroup
	shutil.rmtree(str(vol_image_path))
	shutil.rmtree(str(vol_output_path))


def process_work(work):
	work_local_id = work.split(':')[-1] if ':' in work else work

	for vol_info in get_volume_infos(work):
		print(f'\t[INFO] Volume {vol_info["imagegroup"]} processing ....')

		# save all the images for a given vol
		save_images_for_vol(
			volume_prefix_url=vol_info['volume_prefix_url'],
			work_local_id=work_local_id, 
			imagegroup=vol_info['imagegroup'],
			images_base_dir=IMAGES_BASE_DIR
		)
		print('\t\t- Saved volume images')

		# apply ocr on the vol images
		apply_ocr_on_folder(
			images_base_dir=IMAGES_BASE_DIR,
			work_local_id=work_local_id,
			imagegroup=vol_info['imagegroup'],
			ocr_base_dir=OCR_BASE_DIR
		)
		print('\t\t- Saved volume ocr output')

		# get s3 paths to save images and ocr output
		s3_ocr_paths = get_s3_prefix_path(
			work_local_id=work_local_id,
			imagegroup=vol_info['imagegroup'],
			service=SERVICE,
			batch_prefix=BATCH_PREFIX,
			data_types=[IMAGES, OUTPUT]
		)

		# save image and ocr output at ocr.bdrc.org bucket
		archive_on_s3(
			images_base_dir=IMAGES_BASE_DIR,
			ocr_base_dir=OCR_BASE_DIR,
			work_local_id=work_local_id,
			imagegroup=vol_info['imagegroup'],
			s3_paths=s3_ocr_paths
		)
		print('\t\t- Archived volume images and ocr output')

		# delete current ocred volume
		clean_up(DATA_PATH, work_local_id, vol_info['imagegroup'])
		print('\t\t- Cleaned up volume images and ocr output')

		print(f'\t[INFO] Volume {vol_info["imagegroup"]} completed.')


def get_work_ids(fn):
	for work in fn.read_text().split('\n'):
		if not work: continue
		yield work.strip()


if __name__ == "__main__":
	input_path = Path('usage/bdrc/input')

	for workids_path in input_path.iterdir():
		for work_id in get_work_ids(workids_path):
			print(f'[INFO] Work {work_id} processing ....')
			process_work(work_id)
			print(f'[INFO] Work {work_id} completed.')