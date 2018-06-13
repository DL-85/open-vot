from __future__ import absolute_import, division

import numpy as np
import cv2

from . import Tracker
from ..utils import dict2tuple
from ..utils.complex import real, fft, ifft, complex_mul, complex_div, circ_shift
from ..descriptors.fhog import fast_hog


class TrackerDSST(Tracker):

    def __init__(self, **kargs):
        super(TrackerDSST, self).__init__('DSST')
        self.parse_args(**kargs)

    def parse_args(self, **kargs):
        self.cfg = {
            'padding': 1,
            'output_sigma_factor': 0.0625,
            'scale_sigma_factor': 0.25,
            'lambda_': 1e-2,
            'learning_rate': 0.025,
            'scale_num': 33,
            'scale_step': 1.02,
            'scale_model_max_area': 512}

        for key, val in kargs.items():
            self.cfg.update({key: val})
        self.cfg = dict2tuple(self.cfg)

    def init(self, image, init_rect):
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)

        # initialize parameters
        self.t_center = init_rect[:2] + init_rect[2:] / 2
        self.t_sz = init_rect[2:]
        self.t_scale = 1.0
        self.padded_sz = self.t_sz * (1 + self.cfg.padding)
        self.padded_sz = self.padded_sz.astype(int)

        # create translation gaussian labels
        output_sigma = self.cfg.output_sigma_factor * \
            np.sqrt(np.prod(self.padded_sz)) / (1 + self.cfg.padding)
        rs, cs = np.ogrid[:self.padded_sz[1], :self.padded_sz[0]]
        rs, cs = rs - self.padded_sz[1] // 2, cs - self.padded_sz[0] // 2
        y = np.exp(-0.5 / output_sigma ** 2 * (rs ** 2 + cs ** 2))
        self.yf = fft(y)

        # create scale gaussian labels
        scale_sigma = self.cfg.scale_sigma_factor * \
            np.sqrt(self.cfg.scale_num)
        ss = np.ogrid[:self.cfg.scale_num] - self.cfg.scale_num // 2
        ys = np.exp(-0.5 / scale_sigma ** 2 * (ss ** 2))
        self.ysf = fft(ys)

        # initialize scale factors
        self.scale_factors = self.cfg.scale_step ** ss

        # initialize hanning windows
        self.hann_window = cv2.createHanningWindow(
            tuple(self.padded_sz), cv2.CV_32F)
        self.hann_window = self.hann_window[:, :, np.newaxis]
        self.scale_window = np.float32(np.hanning(self.cfg.scale_num))

        # train translation filter
        z = self._get_translation_sample(
            image, self.t_center, self.padded_sz, self.t_scale)
        self.hf_num, self.hf_den = self._train_translation_filter(z)

        # train scale filter
        zs = self._get_scale_sample(
            image, self.t_center, self.t_sz,
            self.t_scale * self.scale_factors)
        self.sf_num, self.sf_den = self._train_scale_filter(zs)

    def update(self, image):
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)

        # estimate target center
        x = self._get_translation_sample(
            image, self.t_center, self.padded_sz, self.t_scale)
        score = self._calc_score(x, self.hf_num, self.hf_den)
        offset = self._locate_target(score)
        self.t_center += offset

        # estimate target scale
        xs = self._get_scale_sample(
            image, self.t_center, self.t_sz,
            self.t_scale * self.scale_factors)
        score = self._calc_score(xs, self.sf_num, self.sf_den)
        scale_id = score.argmax()
        self.t_scale = self.t_scale * self.scale_factors[scale_id]

        # update translation filter
        z = self._get_translation_sample(
            image, self.t_center, self.padded_sz, self.t_scale)
        hf_num, hf_den = self._train_translation_filter(z)
        self.hf_num = (1 - self.cfg.learning_rate) * self.hf_num + \
            self.cfg.learning_rate * hf_num
        self.hf_den = (1 - self.cfg.learning_rate) * self.hf_den + \
            self.cfg.learning_rate * hf_den
        
        # update scale filter
        zs = self._get_scale_sample(
            image, self.t_center, self.t_sz,
            self.t_scale * self.scale_factors)
        sf_num, sf_den = self._train_scale_filter(zs)
        self.sf_num = (1 - self.cfg.learning_rate) * self.sf_num + \
            self.cfg.learning_rate * sf_num
        self.sf_den = (1 - self.cfg.learning_rate) * self.sf_den + \
            self.cfg.learning_rate * sf_den

        bndbox = np.concatenate([
            self.t_center - self.t_sz * self.t_scale / 2,
            self.t_sz * self.t_scale])

        return bndbox

    def _get_translation_sample(self, image, center, size, scale):
        pass

    def _get_scale_sample(self, image, center, size, scales):
        pass

    def _crop(self, image, center, size):
        corners = np.zeros(4, dtype=int)
        corners[:2] = np.floor(center - size / 2).astype(int)
        corners[2:] = corners[:2] + size
        pads = np.concatenate(
            (-corners[:2], corners[2:] - image.shape[1::-1]))
        pads = np.maximum(0, pads)

        if np.any(pads > 0):
            corners = np.concatenate((
                corners[:2] + pads[:2],
                corners[2:] - pads[2:])).astype(int)

        patch = image[corners[1]:corners[3], corners[0]:corners[2]]

        if np.any(pads > 0):
            patch = cv2.copyMakeBorder(
                patch, pads[1], pads[3], pads[0], pads[2],
                borderType=cv2.BORDER_REPLICATE)

        return patch

    def _extract_feature(self, image):
        return self.hann_window * fast_hog(image, 1, False)

    def _linear_correlation(self, x1, x2):
        xcorr = np.zeros((x1.shape[0], x1.shape[1]), np.float32)
        for i in range(x1.shape[2]):
            xcorr_ = cv2.mulSpectrums(
                fft(x1[:, :, i]), fft(x2[:, :, i]), 0, conjB=True)
            xcorr_ = real(ifft(xcorr_))
            xcorr += xcorr_
        xcorr = circ_shift(xcorr)

        return xcorr / x1.size

    def _locate_target(self, score):
        def subpixel_peak(left, center, right):
            divisor = 2 * center - left - right
            if abs(divisor) < 1e-3:
                return 0
            return 0.5 * (right - left) / divisor

        _, _, _, max_loc = cv2.minMaxLoc(score)
        loc = np.float32(max_loc)

        if max_loc[0] in range(1, score.shape[1] - 1):
            loc[0] += subpixel_peak(
                score[max_loc[1], max_loc[0] - 1],
                score[max_loc[1], max_loc[0]],
                score[max_loc[1], max_loc[0] + 1])
        if max_loc[1] in range(1, score.shape[0] - 1):
            loc[1] += subpixel_peak(
                score[max_loc[1] - 1, max_loc[0]],
                score[max_loc[1], max_loc[0]],
                score[max_loc[1] + 1, max_loc[0]])
        offset = loc - np.float32(score.shape[1::-1]) / 2

        return offset