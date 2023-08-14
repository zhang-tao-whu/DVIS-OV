conda create --name dvis_ov python=3.8 -y
conda activate dvis
pip install torch==1.9.0+cu111 torchvision==0.10.0+cu111 torchaudio==0.9.0 -f https://download.pytorch.org/whl/torch_stable.html
pip install -U opencv-python

# install detectron2
python -m pip install detectron2 -f \
  https://dl.fbaipublicfiles.com/detectron2/wheels/cu111/torch1.9/index.html

# install panoptic api
pip install git+https://github.com/cocodataset/panopticapi.git

git clone git@https://github.com/zhang-tao-whu/DVIS.git
cd DVIS
pip install -r requirements.txt
cd mask2former/modeling/pixel_decoder/ops
sh make.sh
