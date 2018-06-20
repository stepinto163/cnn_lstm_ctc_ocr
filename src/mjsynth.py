# CNN-LSTM-CTC-OCR
# Copyright (C) 2017 Jerod Weinman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import tensorflow as tf
import numpy as np

# The list (well, string) of valid output characters
# If any example contains a character not found here, an error will result
# from the calls to .index in the decoder below
out_charset="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

def num_classes():
    return len(out_charset)

def bucketed_input_pipeline(base_dir,file_patterns,
                            num_threads=4,
                            batch_size=32,
                            boundaries=[32, 64, 96, 128, 160, 192, 224, 256],
                            input_device=None,
                            width_threshold=None,
                            length_threshold=None,
                            num_epoch=None):
    """Get input tensors bucketed by image width
    Returns:
      image : float32 image tensor [batch_size 32 ? 1] padded to batch max width
      width : int32 image widths (for calculating post-CNN sequence length)
      label : Sparse tensor with label sequences for the batch
      length : Length of label sequence (text length)
      text  :  Human readable string for the image
      filename : Source file path
    """

    dataset = _get_dataset(base_dir, file_patterns)

    with tf.device(input_device): # Create bucketing batcher

        training = True

        dataset = dataset.map(lambda element: _parse_function
                              (element, 
                               width_threshold, 
                               length_threshold, 
                               training), num_parallel_calls=num_threads)
        
        dataset = dataset.filter(lambda image, 
                                 width, 
                                 label, 
                                 length, 
                                 text, 
                                 filename, 
                                 get_input: 
                                 get_input != None)

        dataset = dataset.apply(tf.contrib.data.bucket_by_sequence_length
                                (element_length_func=_element_length_fn,
                                 bucket_batch_sizes=np.full
                                 (len(boundaries) + 1, batch_size),
                                 bucket_boundaries=boundaries))
        #TODO potentially add a prefetch after batching (of 1)
        dataset = dataset.apply(tf.contrib.data.shuffle_and_repeat(batch_size, 
                                                                   count=num_epoch))

        dataset = dataset.map(lambda image, 
                              width, label, 
                              length, text, 
                              filename, get_input: 
                              (image, width, 
                              tf.cast(tf.deserialize_many_sparse(label, tf.int64), tf.int32),
                              length, text, filename, get_input))

    return dataset

def threaded_input_pipeline(base_dir,file_patterns,
                            num_threads=4,
                            batch_size=32,
                            batch_device=None,
                            preprocess_device=None):

    training = False
    width_threshold = None
    length_threshold = None

    dataset = _get_dataset(base_dir, file_patterns)

    with tf.device(preprocess_device):
            
        dataset = dataset.map(lambda element: _parse_function
                              (element, 
                               width_threshold, 
                               length_threshold, 
                               training),
                              num_parallel_calls=num_threads)
    
    with tf.device(batch_device): # Create batch queue

        dataset = dataset.batch(batch_size)

        dataset = dataset.map(lambda image, 
                              width, label, 
                              length, text, 
                              filename: 
                              (image, width, 
                              tf.cast(tf.deserialize_many_sparse(label, tf.int64), tf.int32),
                              length, text, filename))

    return dataset

def _element_length_fn(image, width, label, length, text, filename, get_input):
    return width

def _get_input_filter(width, width_threshold, length, length_threshold):
    """Boolean op for discarding input data based on string or image size
    Input:
      width            : Tensor representing the image width
      width_threshold  : Python numerical value (or None) representing the 
                         maximum allowable input image width 
      length           : Tensor representing the ground truth string length
      length_threshold : Python numerical value (or None) representing the 
                         maximum allowable input string length
   Returns:
      keep_input : Boolean Tensor indicating whether to keep a given input 
                  with the specified image width and string length
"""

    keep_input = None

    if width_threshold!=None:
        keep_input = tf.less_equal(width, width_threshold)

    if length_threshold!=None:
        length_filter = tf.less_equal(length, length_threshold)
        if keep_input==None:
            keep_input = length_filter 
        else:
            keep_input = tf.logical_and( keep_input, length_filter)

    if keep_input==None:
        keep_input = True
    else:
        keep_input = tf.reshape( keep_input, [] ) # explicitly make a scalar

    return keep_input

def _get_dataset(base_dir, file_patterns=['*.tfrecord']):
    """Get a data queue for a list of record files"""

    # List of lists ...
    data_files = [tf.gfile.Glob(os.path.join(base_dir,file_pattern))
                  for file_pattern in file_patterns]
    # flatten
    data_files = [data_file for sublist in data_files for data_file in sublist]

    # feed filenames for processing
    dataset = tf.data.TFRecordDataset(data_files)

    return dataset

# https://www.tensorflow.org/programmers_guide/datasets#consuming_tfrecord_data
def _parse_function(data, width_threshold, length_threshold, training):
    """Parse the elements of the dataset"""

    feature_map = {
        'image/encoded':  tf.FixedLenFeature( [], dtype=tf.string, 
                                              default_value='' ),
        'image/labels':   tf.VarLenFeature( dtype=tf.int64 ), 
        'image/width':    tf.FixedLenFeature( [1], dtype=tf.int64,
                                              default_value=1 ),
        'image/filename': tf.FixedLenFeature([], dtype=tf.string,
                                             default_value='' ),
        'text/string':     tf.FixedLenFeature([], dtype=tf.string,
                                              default_value='' ),
        'text/length':    tf.FixedLenFeature( [1], dtype=tf.int64,
                                              default_value=1 )
    }

    features = tf.parse_single_example(data, feature_map)

    image = tf.image.decode_jpeg( features['image/encoded'], channels=1 ) #gray
    width = tf.cast( features['image/width'], tf.int32) # for ctc_loss
    label = tf.serialize_sparse( features['image/labels'] ) # for batching
    length = features['text/length']
    text = features['text/string']
    filename = features['image/filename']

    #Check if input meets a specified standard
    if training:
        keep_input = _get_input_filter(width, width_threshold,
                                       length, length_threshold)
        image = _preprocess_image(image)
        return image,width,label,length,text,filename
    else:
        image = _preprocess_image(image)
        return image,width,label,length,text,filename

    return None

def _preprocess_image(image):
    # Rescale from uint8([0,255]) to float([-0.5,0.5])
    image = tf.image.convert_image_dtype(image, tf.float32)
    image = tf.subtract(image, 0.5)

    # Pad with copy of first row to expand to 32 pixels height
    first_row = tf.slice(image, [0, 0, 0], [1, -1, -1])
    image = tf.concat([first_row, image], 0)

    return image
