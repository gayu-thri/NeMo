# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import glob
import json
import logging
import os
from itertools import repeat
from multiprocessing import Pool

import librosa
import numpy as np
import pandas as pd
from pyannote.core import Annotation, Segment, Timeline
from pyannote.metrics import detection


def prepare_manifest(config):
    """
    Perform VAD on long audio snippet might cause CUDA out of memory issue. 
    Automatically split manifest entry by split_duration to avoid the potential memory issue.
    """
    manifest_vad_input = config.get('manifest_vad_input', "manifest_vad_input.json")
    input_audios = []
    with open(config['manifest_filepath'], 'r') as manifest:
        for line in manifest.readlines():
            input_audios.append(json.loads(line.strip()))

    p = Pool(processes=config['num_workers'])
    args_func = {
        'label': 'infer',
        'split_duration': config['split_duration'],
        'time_length': config['time_length'],
    }
    results = p.starmap(write_vad_infer_manifest, zip(input_audios, repeat(args_func)))
    p.close()

    if os.path.exists(manifest_vad_input):
        logging.info("The prepared manifest file exists. Overwriting!")
        os.remove(manifest_vad_input)

    with open(manifest_vad_input, 'a') as fout:
        for res in results:
            for r in res:
                json.dump(r, fout)
                fout.write('\n')
                fout.flush()

    return manifest_vad_input


def write_vad_infer_manifest(file, args_func):
    """
    Used by prepare_manifest.
    Given a list of files, write them to manifest for dataloader with restrictions.
    Args:
        files : file to be processed
        label : label for audio snippet.
        split_duration : Max duration of each audio clip (each line in json)
        time_length : Used for taking care of joint.
                Length of window for generating the frame.
    Returns:
        res : list of generated metadata line of json for file
    """
    res = []
    label = args_func['label']
    split_duration = args_func['split_duration']
    time_length = args_func['time_length']

    filepath = file['audio_filepath']
    in_duration = file['duration']
    in_offset = file['offset']

    try:
        sr = 16000
        x, _sr = librosa.load(filepath, sr=sr, offset=in_offset, duration=in_duration)
        duration = librosa.get_duration(x, sr=sr)
        left = duration
        current_offset = in_offset

        status = 'single'
        while left > 0:
            if left <= split_duration:
                status = 'end'
                write_duration = left + time_length
                current_offset -= time_length
                offset_inc = left
                left = 0
            else:
                if status == 'start' or status == 'next':
                    status = 'next'
                else:
                    status = 'start'

                if status == 'start':
                    write_duration = split_duration
                    offset_inc = split_duration
                else:
                    write_duration = split_duration + time_length
                    current_offset -= time_length
                    offset_inc = split_duration + time_length

                left -= split_duration

            metadata = {
                'audio_filepath': filepath,
                'duration': write_duration,
                'label': label,
                'text': '_',
                'offset': current_offset,
            }
            res.append(metadata)

            current_offset += offset_inc

    except Exception as e:
        err_file = "error.log"
        with open(err_file, 'w') as fout:
            fout.write(file + ":" + str(e))
    return res


def get_vad_stream_status(data):
    """
    Generate a list of status for each snippet in manifest. A snippet should be in single, start, next or end status. 
    Used for concatenate to full audio file.
    Args:
       data : List of filepath of audio snippet
    Return : list of status of each snippet.
    """
    status = [None] * len(data)
    for i in range(len(data)):
        if i == 0:
            status[i] = 'start' if data[i] == data[i + 1] else 'single'
        elif i == len(data) - 1:
            status[i] = 'end' if data[i] == data[i - 1] else 'single'
        else:
            if data[i] != data[i - 1] and data[i] == data[i + 1]:
                status[i] = 'start'
            elif data[i] == data[i - 1] and data[i] == data[i + 1]:
                status[i] = 'next'
            elif data[i] == data[i - 1] and data[i] != data[i + 1]:
                status[i] = 'end'
            else:
                status[i] = 'single'
    return status


def generate_overlap_vad_seq(frame_pred_dir, smoothing_method, overlap, seg_len, shift_len, num_workers):
    # [TODO] docstring kwargs.

    p = Pool(processes=num_workers)
    frame_filepathlist = glob.glob(frame_pred_dir + "/*.frame")

    overlap_out_dir = frame_pred_dir + "/overlap_smoothing_output" + "_" + smoothing_method + "_" + str(overlap)

    if not os.path.exists(overlap_out_dir):
        os.mkdir(overlap_out_dir)

    # TODO find an elegant way for multiprocessing with multiple arguments
    # TODO change seg_len ,etc. in helper function if necessary

    per_args = {
        "out_dir": overlap_out_dir,
        "method": smoothing_method,
        "overlap": overlap,
        "seg_len": seg_len,
        "shift_len": shift_len,
    }
    p.starmap(generate_overlap_vad_seq_per_file, zip(frame_filepathlist, repeat(per_args)))
    p.close()
    p.join()

    return overlap_out_dir


def generate_overlap_vad_seq_per_file(frame_filepath, per_args):
    """
    Given a frame level prediction, generate predictions with overlapping input segments by using it
    Args:
        frame_filepath : frame prediction file to be processed.
        per_args:
            method : Median or mean smoothing filter.
            overlap : Amounts of overlap.
            seg_len : Length of window for generating the frame.
            shift_len : Amount of shift of window for generating the frame.
            out_dir : Output dir of generated prediction.
    """

    try:
        method = per_args['method']
        overlap = per_args['overlap']
        seg_len = per_args['seg_len']
        shift_len = per_args['shift_len']
        out_dir = per_args['out_dir']

        frame = np.loadtxt(frame_filepath)
        name = os.path.basename(frame_filepath).split(".frame")[0] + "." + method
        overlap_filepath = os.path.join(out_dir, name)

        shift = int(shift_len / 0.01)  # number of units of shift
        seg = int((seg_len / 0.01 + 1))  # number of units of each window/segment

        jump_on_target = int(seg * (1 - overlap))  # jump on target generated sequence
        jump_on_frame = int(jump_on_target / shift)  # jump on input frame sequence

        if jump_on_frame < 1:
            raise ValueError(
                f"Note we jump over frame sequence to generate overlapping input segments. \n \
            Your input makes jump_on_fram={jump_on_frame} < 1 which is invalid because it cannot jump and will stuck.\n \
            Please try different seg_len, shift_len and overlap choices. \n \
            jump_on_target = int(seg * (1 - overlap)) \n \
            jump_on_frame  = int(jump_on_frame/shift) "
            )

        target_len = int(len(frame) * shift)

        if method == 'mean':
            preds = np.zeros(target_len)
            pred_count = np.zeros(target_len)

            for i, og_pred in enumerate(frame):
                if i % jump_on_frame != 0:
                    continue
                start = i * shift
                end = start + seg
                preds[start:end] = preds[start:end] + og_pred
                pred_count[start:end] = pred_count[start:end] + 1

            preds = preds / pred_count
            last_non_zero_pred = preds[pred_count != 0][-1]
            preds[pred_count == 0] = last_non_zero_pred

        elif method == 'median':
            preds = [[] for _ in range(target_len)]
            for i, og_pred in enumerate(frame):
                if i % jump_on_frame != 0:
                    continue

                start = i * shift
                end = start + seg
                for j in range(start, end):
                    if j <= target_len - 1:
                        preds[j].append(og_pred)

            preds = np.array([np.median(l) for l in preds])
            nan_idx = np.isnan(preds)
            last_non_nan_pred = preds[~nan_idx][-1]
            preds[nan_idx] = last_non_nan_pred

        else:
            raise ValueError("method should be either mean or median")

        round_final = np.round(preds, 4)
        np.savetxt(overlap_filepath, round_final, delimiter='\n')

    except Exception as e:
        raise (e)


def generate_vad_segment_table(
    vad_pred_dir, threshold, shift_len, num_workers,
):
    p = Pool(processes=num_workers)
    suffixes = ("frame", "mean", "median")
    vad_pred_filepath_list = [os.path.join(vad_pred_dir, x) for x in os.listdir(vad_pred_dir) if x.endswith(suffixes)]

    table_out_dir = os.path.join(vad_pred_dir, "table_output_" + str(threshold))
    if not os.path.exists(table_out_dir):
        os.mkdir(table_out_dir)

    per_args = {
        "threshold": threshold,
        "shift_len": shift_len,
        "out_dir": table_out_dir,
    }

    p.starmap(generate_vad_segment_table_per_file, zip(vad_pred_filepath_list, repeat(per_args)))
    p.close()
    p.join()

    return table_out_dir


def generate_vad_segment_table_per_file(pred_filepath, per_args):
    """
    Convert frame level prediction to speech/no-speech segment in start and end times format.
    And save to csv file  in rttm-like format
            0, 10, speech
            10,12, no-speech
    Args:
        pred_filepath : prediction file to be processed.
        per_args :
            threshold : threshold for prediction score (from 0 to 1).
            shift_len : Amount of shift of window for generating the frame.
            out_dir : Output dir of generated table/csv file.
    """
    threshold = per_args['threshold']
    shift_len = per_args['shift_len']
    out_dir = per_args['out_dir']

    name = pred_filepath.split("/")[-1].rsplit(".", 1)[0]

    sequence = np.loadtxt(pred_filepath)
    start = 0
    end = 0
    start_list = [0]
    dur_list = []
    state_list = []
    current_state = "non-speech"
    for i in range(len(sequence) - 1):
        current_state = "non-speech" if sequence[i] <= threshold else "speech"
        next_state = "non-speech" if sequence[i + 1] <= threshold else "speech"
        if next_state != current_state:
            dur = i * shift_len + shift_len - start  # shift_len for handling joint
            state_list.append(current_state)
            dur_list.append(dur)

            start = (i + 1) * shift_len
            start_list.append(start)

    dur_list.append((i + 1) * shift_len + shift_len - start)
    state_list.append(current_state)

    seg_table = pd.DataFrame({'start': start_list, 'dur': dur_list, 'vad': state_list})

    save_name = name + ".txt"
    save_path = os.path.join(out_dir, save_name)
    seg_table.to_csv(save_path, sep='\t', index=False, header=False)
    return save_path


def write_vad_pred_to_manifest(vad_directory, audio_directory, manifest_file):
    vad_files = glob.glob(vad_directory + "/*.txt")
    with open(manifest_file, 'w') as outfile:
        for vad_file in vad_files:
            f = open(vad_file, 'r')
            lines = f.readlines()
            audio_name = os.path.basename(vad_file).split('.')[0]
            for line in lines:
                vad_out = line.strip().split()
                start, dur, activity = float(vad_out[0]), float(vad_out[1]) - float(vad_out[0]), vad_out[2]
                start, dur = float("{:.3f}".format(start)), float("{:.3f}".format(dur))
                if activity.lower() == 'speech':
                    audio_path = os.path.join(audio_directory, audio_name + '.wav')
                    meta = {"audio_filepath": audio_path, "offset": start, "duration": dur, "label": 'UNK'}
                    json.dump(meta, outfile)
                    outfile.write("\n")

            f.close()


# TODO reuse/merge  Nithin's code in speaker_utils
def vad_construct_pyannote_object_per_file(vad_table_filepath, groundtruth_RTTM_file):

    pred = pd.read_csv(vad_table_filepath, sep="\t", header=None)
    label = pd.read_csv(groundtruth_RTTM_file, sep=" ", delimiter=None, header=None)
    label = label.rename(columns={3: "start", 4: "dur", 7: "speaker"})

    # construct reference
    reference = Annotation()
    for index, row in label.iterrows():
        reference[Segment(row['start'], row['start'] + row['dur'])] = row['speaker']

    # construct hypothsis
    hypothesis = Annotation()
    for index, row in pred.iterrows():
        if row[2] == 'speech':
            hypothesis[Segment(row[0], row[1])] = 'Speech'
    return reference, hypothesis


def vad_tune_threshold_on_dev(thresholds, vad_pred_dir, groundtruth_RTTM_dir):
    threshold_perf = {}
    best_threhsold = thresholds[0]
    for threshold in thresholds:
        min_der = 1
        metric = detection.DetectionErrorRate()
        filenames = [
            os.path.basename(f).split(".")[0] for f in glob.glob(os.path.join(groundtruth_RTTM_dir, "*.rttm"))
        ]
        for filename in filenames:
            vad_pred_filepath = os.path.join(vad_pred_dir, filename + '.median')
            table_out_dir = os.path.join(vad_pred_dir, "table_output_" + str(threshold))

            if not os.path.exists(table_out_dir):
                os.mkdir(table_out_dir)
            per_args = {"threshold": threshold, "shift_len": 0.01, "out_dir": table_out_dir}

            vad_table_filepath = generate_vad_segment_table_per_file(vad_pred_filepath, per_args)
            groundtruth_RTTM_file = os.path.join(groundtruth_RTTM_dir, filename + '.rttm')

            reference, hypothesis = vad_construct_pyannote_object_per_file(vad_table_filepath, groundtruth_RTTM_file)
            metric(reference, hypothesis)  # accumulation
        report = metric.report(display=False)
        DetER = report.iloc[[-1]][('detection error rate', '%')].item()
        FA = (report.iloc[[-1]][('false alarm', '%')].item(),)
        MISS = report.iloc[[-1]][('miss', '%')].item()
        threshold_perf[threshold] = {'DetER': DetER, 'FA': FA, 'MISS': MISS}
        print(threshold, threshold_perf[threshold])
        del report
        metric.reset()  # reset internal accumulator
        if DetER < min_der:
            min_der = DetER
            best_threhsold = threshold
    # return threshold with smallest der [TODO] return full result to user for flexible use
    return best_threhsold
