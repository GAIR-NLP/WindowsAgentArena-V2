#!/bin/bash

export CUDA_VISIBLE_DEVICES=0,1,2,3

vllm serve "henryhe0123/PC-Agent-E" --tensor-parallel-size 4 --port 8030 
