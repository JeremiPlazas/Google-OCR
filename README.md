# Google-OCR

### Setup with pip & virtualenv
Clone this repo and follow the steps below
```
cd Google-OCR
virtualenv .env
source .env/bin/activate
pip install -r requirements.txt
```
Follow this [Quick Start](https://pypi.org/project/google-cloud-vision/) guide to setup Google Vision API, which is necessary for using Google OCR service. There is also [video tutorial](https://www.youtube.com/watch?v=nMY0qDg16y4)


### Usage
Running OCR on collection of images. Note: This script only works on png image, so be sure to convert PDF or other format into png. But we will be releasing support for other format in future. 
```
cd ocr
python google_ocr.py --input_dir data/W22084/vol-1 --output_dir W22084/vol-1 --n 3
```
Output of OCR will be stored in txt file at output_dir.
