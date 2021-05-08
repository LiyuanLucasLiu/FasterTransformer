docker run -ti --name faster --gpus all nvcr.io/nvidia/pytorch:20.12-py3 bash

git clone https://github.com/LiyuanLucasLiu/FasterTransformer.git
mkdir -p build
cd build

cmake -DSM=75 -DCMAKE_BUILD_TYPE=Release -DBUILD_PYT=ON ..
make

pip install transformers==2.5.1

python pytorch/encoder_sample.py 32 12 32 12 64 --fp16 --time
FF: 6.96ms

python pytorch/encoder_sample.py 32 144 32 1 64 --fp16 --time --size_ratio_to_full 12
FF: 7.96ms
