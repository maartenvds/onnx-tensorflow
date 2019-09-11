from __future__ import division

import tensorflow as tf
from numpy import inf
import numpy as np


def _tf_shape(a):
    if a.shape.is_fully_defined():
        return np.array(a.shape.as_list(), dtype=np.int64)
    else:
        return tf.shape(a, out_type=tf.int64)


class DilatedPooling(object):
    def __init__(self, input, kernel_shape, strides, dilations,
                 padding="VALID", ceil_mode=False, pooling_type="MAX"):
        self.kernel_shape = kernel_shape
        self.strides = strides
        self.dilations = dilations
        self.padding = padding
        self.ceil_mode = ceil_mode
        self.pooling_type = pooling_type

        self.is_known_shape = input.shape.is_fully_defined()
        self.spatial_size = len(kernel_shape)
        self.input_rank = self.spatial_size + 2

        # if the rank is not defined, set it to the calculated input_rank
        # rank should be known for ops like tf.gather_nd
        if not input.shape.rank:
            input.set_shape([None] * self.input_rank)
        self.orig_input_shape = _tf_shape(input)
        self.input_shape = self.orig_input_shape
        self.input = input

        if pooling_type == "MAX":
            self.padding_constant = -inf
        else:
            self.padding_constant = 0

    def _calc_input_ind(self, ind, k, d, s):
        """
            This function maps index from the output of _reduce_dilations
            to index from the original input inside single axis

            Args:
                ind:      vector with indices from the output to be mapped
                k:        tuple with kernel size along the axis
                d:        tuple with dilations
                s:        tuple with strides
            Return:
                input_ind: calculated index in the original input

            The formula is:
                input_ind = (ind // kernel) * stride + (ind % kernel) *
                            dilation

            Example:
              If we have following input to _reduce_dilations:
                         [[  0,  1,  2,  3],
                          [  4,  5,  6,  7],
                          [  8,  9, 10, 11],
                          [ 12, 13, 14, 15]]
              and Kernel = [2, 2], Dilations: [2, 2], Strides: [1, 1]

              the output dilated_pool shape will be [4, 4] and _calc_input_ind
              will be called twice for the two axis 0 (along height) and
              1 (along width) with

                  ind = [0, 1, 2, 3]

              which will result in:

                  input_ind = [0, 2, 1, 3]
        """
        return (ind // k) * (s - k * d) + ind * d

    def _calc_orig_ind(self, ind):
        """
            Map result indices to the original input indices

            Maps indices generated by maxpool_with_argmax on top of the
            dilation reduced input to the orignal input indices
        """

        in_width = self.orig_input_shape[2]
        num_channels = self.orig_input_shape[3]
        output_width = self.output_shape[2]

        # mod_floor op is not implemented on GPU
        # implement it using: a % b = a - (a // b) * b

        # inRow = (ind // num_channels) // output_width
        # inCol = (ind // num_channels) % output_width
        # ind_channel = ind % num_channels

        ind_ = ind // num_channels
        inRow = ind_ // output_width
        inCol = ind_ - (ind_ // output_width) * output_width

        ind_channel = ind - ind_ * num_channels

        row = self._calc_input_ind(inRow, self.kernel_shape[0],
                                   self.dilations[0],
                                   self.strides[0]) - self.pads[0]
        col = self._calc_input_ind(inCol, self.kernel_shape[1],
                                   self.dilations[1],
                                   self.strides[1]) - self.pads[2]

        new_ind = num_channels * (row * in_width + col) + ind_channel
        return new_ind

    def _tf_product(self, a, b):
        """
            Calculates the cartesian product of two vectors a and b
        """
        tile_b = tf.tile(tf.expand_dims(b, 1), [1, tf.shape(a)[0]])
        tile_b = tf.expand_dims(tile_b, 2)
        tile_b = tf.reshape(tile_b, [-1, 1])

        a = tf.tile(a, [tf.shape(b)[0], 1])
        a = tf.concat([tile_b, a], axis=1)

        return a

    def _reduce_dilations(self):
        """
            This method reduces the dilations by extracting the values from
            the input for every sliding window according to the dilations,
            strides and kernel size and generates output that can be used by
            pooling operation with strides = kernel_shape to accomplish
            dilated pooling

            Example:
              Input:     [[  0,  1,  2,  3],
                          [  4,  5,  6,  7],
                          [  8,  9, 10, 11],
                          [ 12, 13, 14, 15]]

              Kernel:    [2, 2]
              Dilations: [2, 2]
              Strides:   [1, 1]

              Will return:
                         [[  0,  2,  1,  3],
                          [  8, 10,  9, 11],
                          [  4,  6,  5,  7],
                          [ 12, 14, 13, 15]]

              After max_pool2d with kernel_shape = strides = [2, 2]
              the result is:
                         [[ 10, 11],
                          [ 14, 15]]

              The method will also pad the input according to the paddings
              provided and if ceil_mode is enabled
        """

        input_shape = _tf_shape(self.input)
        in_spatial_shape = input_shape[1:self.spatial_size+1]

        channel_num = input_shape[self.spatial_size+1]
        gather_ind = tf.range(channel_num, dtype=tf.int64)
        gather_ind = tf.expand_dims(gather_ind, 1)

        self.output_shape = [input_shape[0]]
        for dim in range(self.spatial_size - 1, -1, -1):
            filter_size = (self.kernel_shape[dim] - 1) * \
                           self.dilations[dim] + 1
            output_size = (((in_spatial_shape[dim] - filter_size) //
                           self.strides[dim]) + 1) * self.kernel_shape[dim]
            self.output_shape += [output_size]
            local_ind = tf.range(output_size)
            local_ind = self._calc_input_ind(local_ind, self.kernel_shape[dim],
                                             self.dilations[dim],
                                             self.strides[dim])

            gather_ind = self._tf_product(gather_ind, local_ind)
        self.output_shape += [channel_num]

        for x in range(self.spatial_size):
            gather_ind = tf.expand_dims(gather_ind, 0)
        gather_ind = tf.tile(gather_ind, [input_shape[0]] + [1] *
                             (self.spatial_size + 1))

        # extract the selected values from the input
        output = tf.gather_nd(self.input, gather_ind, batch_dims=1)
        output = tf.reshape(output, self.output_shape)

        return output

    def _calc_pads_same(self, in_spatial_shape):
        """
            Calculate SAME_* paddings
        """

        pads = []
        for i in range(self.spatial_size):
            in_size = in_spatial_shape[i]
            filter_size = (self.kernel_shape[i] - 1) * self.dilations[i] + 1

            if self.is_known_shape:
                maximum_op = np.maximum
                ceil_op = np.ceil
                floor_op = np.floor
            else:
                maximum_op = tf.maximum
                ceil_op = tf.ceil
                floor_op = tf.floor

            out_size = ceil_op(in_size / self.strides[i])
            pad_along_axis = maximum_op((out_size - 1) * self.strides[i] +
                                        filter_size - in_size, 0)
            if self.padding.lower() == "same_lower":
                pad_op = ceil_op
            else:
                pad_op = floor_op
            pad_begin = pad_op(pad_along_axis / 2)
            if self.is_known_shape:
                pad_begin = pad_begin.astype(np.int64)
                pad_along_axis = pad_along_axis.astype(np.int64)
            else:
                pad_begin = tf.cast(pad_begin, tf.int64)
                pad_along_axis = tf.cast(pad_along_axis, tf.int64)
            pad_end = pad_along_axis - pad_begin

            pads += [pad_begin, pad_end]

        return pads

    def _calc_pads_explicit(self):
        """
            Calculate explicit padding
        """
        assert type(self.padding) is list

        pads = []
        for i in range(self.spatial_size):
            pads += [self.padding[i], self.padding[i + self.spatial_size]]
        return pads

    def _calc_pads_ceil_mode(self, in_spatial_shape):
        """
            Calculate padding in ceil_mode
        """

        pads = []
        for i in range(self.spatial_size):
            dim_size = in_spatial_shape[i]
            filter_size = (self.kernel_shape[i] - 1) * self.dilations[i] + 1
            out_size = (dim_size - filter_size) / self.strides[i]
            if self.is_known_shape:
                pad_size = (np.ceil(out_size) -
                            np.floor(out_size)).astype(np.int64)
            else:
                pad_size = tf.cast(tf.math.ceil(out_size) -
                                   tf.math.floor(out_size), tf.int64)

            pads += [0, pad_size * self.strides[i]]
        return pads

    def _calc_pads(self, in_spatial_shape):
        if self.is_known_shape:
            pads = np.zeros([self.spatial_size * 2], np.int64)
        else:
            pads = tf.zeros([self.spatial_size * 2], tf.int64)

        # check for explicit padding
        if type(self.padding) is list:
            pads += self._calc_pads_explicit()
        elif self.padding.lower().startswith("same"):
            pads += self._calc_pads_same(in_spatial_shape)

        # when padding is set to SAME, ceil_mode will not do anything
        # because output sizes will be multiple of the strides
        if self.ceil_mode and (type(self.padding) is list or
                               not self.padding.lower().startswith("same")):
            new_spatial_shape = [in_spatial_shape[i] + pads[i * 2] +
                                 pads[i * 2 + 1] for i in
                                 range(self.spatial_size)]
            pads += self._calc_pads_ceil_mode(new_spatial_shape)
        return pads

    def _pad_input(self):
        """
            Pad the input according to the parameters
        """
        # check if we need to do any padding at all
        if not self.ceil_mode and ((type(self.padding) is list and
                                   self.padding == [0] * self.spatial_size * 2)
                                   or self.padding == "VALID"):
            self.pads = np.array([0] * self.spatial_size * 2)
            return (self.input, self.pads)

        in_spatial_shape = self.input_shape[1:self.spatial_size + 1]
        pads = self._calc_pads(in_spatial_shape)

        if self.is_known_shape and np.count_nonzero(pads) == 0:
            self.pads = pads
            return (self.input, pads)

        tf_paddings = [[0, 0]]
        for i in range(self.spatial_size):
            tf_paddings += [[pads[i * 2], pads[i * 2 + 1]]]
        tf_paddings += [[0, 0]]

        self.input = tf.pad(self.input, tf_paddings, mode='CONSTANT',
                            constant_values=self.padding_constant)
        # update input shape and pads values
        self.input_shape = _tf_shape(self.input)
        self.pads = pads

    def _calc_argmax_without_padding(self, ind):
        """
            Calculate the original indices as they would be without padding
        """
        in_width = self.orig_input_shape[2]
        padded_width = self.input_shape[2]
        num_channels = self.input_shape[3]

        # mod_floor op is not implemented on GPU
        # implement it using: a % b = a - (a // b) * b

        # ind_ = ind // num_channels
        # ind_channel = ind % num_channels

        ind_ = ind // num_channels
        ind_channel = ind - ind_ * num_channels

        ind__ = (ind_ // padded_width) * (self.pads[2] + self.pads[3])
        ind__ = ind_ - ind__ - self.pads[0] * in_width - self.pads[2]
        ind__ = num_channels * ind__ + ind_channel
        return ind__

    def dilated_maxpool_with_argmax(self, force_custom_impl=False):
        """
            Do a dilated maxpool and return indices/argmax
        """
        # Tensorflow does not support maxpool_with_argmax on
        # spatial_size != 2
        assert self.spatial_size == 2

        if list(self.dilations) != [1] * self.spatial_size or \
           force_custom_impl:
            # pad the input
            self._pad_input()

            new_input = self._reduce_dilations()
            kernel_shape = [1] + list(self.kernel_shape) + [1]
            pooled, new_ind = tf.nn.max_pool_with_argmax(
                                    new_input, ksize=kernel_shape,
                                    strides=kernel_shape, padding="VALID")
            new_ind = self._calc_orig_ind(new_ind)
        else:
            if type(self.padding) is list or \
               self.padding.lower() == "same_lower":
                # pad the input
                self._pad_input()

                padding_ = "VALID"
            elif self.padding.lower() == "same_upper":
                padding_ = "SAME"
            else:
                padding_ = self.padding

            strides = [1] + list(self.strides) + [1]
            kernel_shape = [1] + list(self.kernel_shape) + [1]
            pooled, new_ind = tf.nn.max_pool_with_argmax(
                                    self.input, ksize=kernel_shape,
                                    strides=strides, padding=padding_)
            # if there was padding, recalculate the returned index
            # to exclude the padding
            if np.count_nonzero(self.pads) != 0:
                new_ind = self._calc_argmax_without_padding(new_ind)

        return (pooled, new_ind)

    def dilated_maxpool(self, force_custom_impl=False):
        """
            Does N-D dilated max pooling. Pads the input if explicit or
            SAME_* padding is provided or ceil_mode is True
        """

        if type(self.padding) is list or self.padding.lower() == "same_lower":
            # pad the input
            self._pad_input()

            padding_ = "VALID"
        elif self.padding.lower() == "same_upper":
            padding_ = "SAME"
        else:
            padding_ = self.padding

        # if spatial_size == 2 we can use tf.nn.dilation2d directly
        if self.spatial_size == 2 and not force_custom_impl:
            strides = [1] + list(self.strides) + [1]
            dilations = [1] + list(self.dilations) + [1]

            filter = tf.zeros([self.kernel_shape[0], self.kernel_shape[1],
                              self.input_shape[3]], self.input.dtype)
            pooled = tf.nn.dilation2d(input=self.input, filter=filter,
                                      strides=strides, rates=dilations,
                                      padding=padding_)
        # if strides == [1] * spatial_size or dilation == [1] * spatial_size we
        # can use tf.nn.pool
        elif self.strides == [1] * self.spatial_size or \
                self.dilations == [1] * self.spatial_size and \
                not force_custom_impl:
            pooled = tf.nn.pool(self.input, window_shape=self.kernel_shape,
                                dilation_rate=self.dilations,
                                strides=self.strides, padding=padding_,
                                pooling_type="MAX")
        # in any other case we use custom implementation _reduce_dilations
        # to reduce atrous/dilated pooling into regular pooling and selecting
        # only the values of the input that should have been selected by
        # applying the strides and dilations. Then use tf.nn.pool with
        # strides = kernel_shape and no dilations
        else:
            if padding_ == "SAME":
                # pad the input
                self._pad_input()

            input_ = self._reduce_dilations()
            pooled = tf.nn.pool(input_, window_shape=self.kernel_shape,
                                strides=self.kernel_shape, padding="VALID",
                                pooling_type=self.pooling_type)
        return pooled
