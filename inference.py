import os
import sys
import time
import argparse
import logging
import math
import gc
import decord
import json

import numpy as np
import mxnet as mx
from mxnet import nd
from mxnet.gluon.data.vision import transforms
from gluoncv.data.transforms import video
from gluoncv.model_zoo import get_model
from gluoncv.data import VideoClsCustom
from gluoncv.utils import makedirs

def parse_args():
    parser = argparse.ArgumentParser(description='Make predictions on your own videos.')
    parser.add_argument('--data-dir', type=str, default='',
                        help='the root path to your data')
    parser.add_argument('--need-root', action='store_true',
                        help='if set to True, --data-dir needs to be provided as the root path to find your videos.')
    parser.add_argument('--data-list', type=str, default='',
                        help='the list of your data')
    parser.add_argument('--dtype', type=str, default='float32',
                        help='data type for training. default is float32')
    parser.add_argument('--gpu-id', type=int, default=0,
                        help='number of gpus to use.')
    parser.add_argument('--mode', type=str,
                        help='mode in which to train the model. options are symbolic, imperative, hybrid')
    parser.add_argument('--model', type=str, required=True,
                        help='type of model to use. see vision_model for options.')
    parser.add_argument('--input-size', type=int, default=224,
                        help='size of the input image size. default is 224')
    parser.add_argument('--use-pretrained', action='store_true', default=True,
                        help='enable using pretrained model from gluon.')
    parser.add_argument('--hashtag', type=str, default='',
                        help='hashtag for pretrained models.')
    parser.add_argument('--resume-params', type=str, default='',
                        help='path of parameters to load from.')
    parser.add_argument('--log-interval', type=int, default=10,
                        help='Number of batches to wait before logging.')
    parser.add_argument('--new-height', type=int, default=256,
                        help='new height of the resize image. default is 256')
    parser.add_argument('--new-width', type=int, default=340,
                        help='new width of the resize image. default is 340')
    parser.add_argument('--new-length', type=int, default=32,
                        help='new length of video sequence. default is 32')
    parser.add_argument('--new-step', type=int, default=1,
                        help='new step to skip video sequence. default is 1')
    parser.add_argument('--num-classes', type=int, default=400,
                        help='number of classes.')
    parser.add_argument('--ten-crop', action='store_true',
                        help='whether to use ten crop evaluation.')
    parser.add_argument('--three-crop', action='store_true',
                        help='whether to use three crop evaluation.')
    parser.add_argument('--video-loader', action='store_true', default=True,
                        help='if set to True, read videos directly instead of reading frames.')
    parser.add_argument('--use-decord', action='store_true', default=True,
                        help='if set to True, use Decord video loader to load data. Otherwise use mmcv video loader.')
    parser.add_argument('--slowfast', action='store_true',
                        help='if set to True, use data loader designed for SlowFast network.')
    parser.add_argument('--slow-temporal-stride', type=int, default=16,
                        help='the temporal stride for sparse sampling of video frames for slow branch in SlowFast network.')
    parser.add_argument('--fast-temporal-stride', type=int, default=2,
                        help='the temporal stride for sparse sampling of video frames for fast branch in SlowFast network.')
    parser.add_argument('--num-crop', type=int, default=1,
                        help='number of crops for each image. default is 1')
    parser.add_argument('--num-segments', type=int, default=1,
                        help='number of segments to evenly split the video.')
    parser.add_argument('--save-dir', type=str, default='./predictions',
                        help='directory of saved results')
    parser.add_argument('--logging-file', type=str, default='predictions.log',
                        help='name of predictions log file')
    parser.add_argument('--save-logits', action='store_true',
                        help='if set to True, save logits to .npy file for each video.')
    parser.add_argument('--save-preds', action='store_true',
                        help='if set to True, save predictions to .npy file for each video.')
    opt = parser.parse_args()
    return opt

def sample_indices(opt, num_frames):
    if num_frames > opt.skip_length - 1:
        tick = (num_frames - opt.skip_length + 1) / \
            float(opt.num_segments)
        offsets = np.array([int(tick / 2.0 + tick * x)
                            for x in range(opt.num_segments)])
    else:
        offsets = np.zeros((opt.num_segments,))

    skip_offsets = np.zeros(opt.skip_length // opt.new_step, dtype=int)
    return offsets + 1, skip_offsets

def video_TSN_decord_batch_loader(opt, directory, video_reader, duration, indices, skip_offsets):
        sampled_list = []
        frame_id_list = []
        for seg_ind in indices:
            offset = int(seg_ind)
            for i, _ in enumerate(range(0, opt.skip_length, opt.new_step)):
                if offset + skip_offsets[i] <= duration:
                    frame_id = offset + skip_offsets[i] - 1
                else:
                    frame_id = offset - 1
                frame_id_list.append(frame_id)
                if offset + opt.new_step < duration:
                    offset += opt.new_step
        try:
            video_data = video_reader.get_batch(frame_id_list).asnumpy()
            sampled_list = [video_data[vid, :, :, :] for vid, _ in enumerate(frame_id_list)]
        except:
            raise RuntimeError('Error occured in reading frames {} from video {} of duration {}.'.format(frame_id_list, directory, duration))
        return sampled_list

def video_TSN_decord_slowfast_loader(opt, directory, video_reader, duration, indices, skip_offsets):
    sampled_list = []
    frame_id_list = []
    for seg_ind in indices:
        fast_id_list = []
        slow_id_list = []
        offset = int(seg_ind)
        for i, _ in enumerate(range(0, opt.skip_length, opt.new_step)):
            if offset + skip_offsets[i] <= duration:
                frame_id = offset + skip_offsets[i] - 1
            else:
                frame_id = offset - 1

            if (i + 1) % opt.fast_temporal_stride == 0:
                fast_id_list.append(frame_id)

                if (i + 1) % opt.slow_temporal_stride == 0:
                    slow_id_list.append(frame_id)

            if offset + opt.new_step < duration:
                offset += opt.new_step

        fast_id_list.extend(slow_id_list)
        frame_id_list.extend(fast_id_list)
    try:
        video_data = video_reader.get_batch(frame_id_list).asnumpy()
        sampled_list = [video_data[vid, :, :, :] for vid, _ in enumerate(frame_id_list)]
    except:
        raise RuntimeError('Error occured in reading frames {} from video {} of duration {}.'.format(frame_id_list, directory, duration))
    return sampled_list

def read_data(opt, video_name, transform):

    decord_vr = decord.VideoReader(video_name, width=opt.new_width, height=opt.new_height)
    duration = len(decord_vr)

    opt.skip_length = opt.new_length * opt.new_step
    segment_indices, skip_offsets = sample_indices(opt, duration)

    if opt.video_loader:
        if opt.slowfast:
            clip_input = video_TSN_decord_slowfast_loader(opt, video_name, decord_vr, duration, segment_indices, skip_offsets)
        else:
            clip_input = video_TSN_decord_batch_loader(opt, video_name, decord_vr, duration, segment_indices, skip_offsets)

    clip_input = transform(clip_input)

    if opt.slowfast:
        sparse_sampels = len(clip_input) // (opt.num_segments * opt.num_crop)
        clip_input = np.stack(clip_input, axis=0)
        clip_input = clip_input.reshape((-1,) + (sparse_sampels, 3, opt.input_size, opt.input_size))
        clip_input = np.transpose(clip_input, (0, 2, 1, 3, 4))
    else:
        clip_input = np.stack(clip_input, axis=0)
        clip_input = clip_input.reshape((-1,) + (opt.new_length, 3, opt.input_size, opt.input_size))
        clip_input = np.transpose(clip_input, (0, 2, 1, 3, 4))

    if opt.new_length == 1:
        clip_input = np.squeeze(clip_input, axis=2)    # this is for 2D input case

    return nd.array(clip_input)

def main(logger):
    opt = parse_args()

    makedirs(opt.save_dir)

    filehandler = logging.FileHandler(os.path.join(opt.save_dir, opt.logging_file))
    streamhandler = logging.StreamHandler()
    logger = logging.getLogger('')
    logger.setLevel(logging.INFO)
    logger.addHandler(filehandler)
    logger.addHandler(streamhandler)
    logger.info(opt)

    gc.set_threshold(100, 5, 5)

    # set env
    gpu_id = opt.gpu_id
    # context = mx.gpu(gpu_id)
    context = mx.cpu()
    # get data preprocess
    image_norm_mean = [0.485, 0.456, 0.406]
    image_norm_std = [0.229, 0.224, 0.225]
    if opt.ten_crop:
        transform_test = transforms.Compose([
            video.VideoTenCrop(opt.input_size),
            video.VideoToTensor(),
            video.VideoNormalize(image_norm_mean, image_norm_std)
        ])
        opt.num_crop = 10
    elif opt.three_crop:
        transform_test = transforms.Compose([
            video.VideoThreeCrop(opt.input_size),
            video.VideoToTensor(),
            video.VideoNormalize(image_norm_mean, image_norm_std)
        ])
        opt.num_crop = 3
    else:
        transform_test = video.VideoGroupValTransform(size=opt.input_size, mean=image_norm_mean, std=image_norm_std)
        opt.num_crop = 1

    # get model
    if opt.use_pretrained and len(opt.hashtag) > 0:
        opt.use_pretrained = opt.hashtag
    classes = opt.num_classes
    model_name = opt.model
    net = get_model(name=model_name, nclass=classes, pretrained=opt.use_pretrained,
                    num_segments=opt.num_segments, num_crop=opt.num_crop)
    net.cast(opt.dtype)
    net.collect_params().reset_ctx(context)
    if opt.mode == 'hybrid':
        net.hybridize(static_alloc=True, static_shape=True)
    if opt.resume_params is not '' and not opt.use_pretrained:
        net.load_parameters(opt.resume_params, ctx=context)
        logger.info('Pre-trained model %s is successfully loaded.' % (opt.resume_params))
    else:
        logger.info('Pre-trained model is successfully loaded from the model zoo.')
    logger.info("Successfully built model {}".format(model_name))

    # get data
    anno_file = opt.data_list
    f = open(anno_file, 'r')
    data_list = f.readlines()
    logger.info('Load %d video samples.' % len(data_list))

    start_time = time.time()
    for vid, vline in enumerate(data_list):
        video_path = vline.split()[0]
        video_name = video_path.split('/')[-1]
        if opt.need_root:
            video_path = os.path.join(opt.data_dir, video_path)
        video_data = read_data(opt, video_path, transform_test)
        video_input = video_data.as_in_context(context)
        pred = net(video_input.astype(opt.dtype, copy=False))
        if opt.save_logits:
            logits_file = '%s_%s_logits.npy' % (model_name, video_name)
            np.save(os.path.join(opt.save_dir, logits_file), pred.asnumpy())
        pred_label = np.argmax(pred.asnumpy())
        if opt.save_preds:
            preds_file = '%s_%s_preds.npy' % (model_name, video_name)
            np.save(os.path.join(opt.save_dir, preds_file), pred_label)

        logger.info('%04d/%04d: %s is predicted to class %d' % (vid, len(data_list), video_name, pred_label))

    end_time = time.time()
    logger.info('Total inference time is %4.2f minutes' % ((end_time - start_time) / 60))

if __name__ == '__main__':
    logging.basicConfig()
    logger = logging.getLogger('logger')
    logger.setLevel(logging.INFO)

    main(logger)
