import numpy as np
from champ import misc
import logging

log = logging.getLogger(__name__)


class ImageData(object):
    """A class for image data to be correlated with fastq coordinate data."""
    def __init__(self, filename, um_per_pixel, image):
        assert isinstance(image, np.ndarray), 'Image not numpy ndarray'
        self.fname = str(filename)
        self.fft = None
        self.image = image
        self.median_normalize()
        self.um_per_pixel = um_per_pixel
        self.um_dims = self.um_per_pixel * np.array(self.image.shape)

    def median_normalize(self):
        med = np.median(self.image)
        self.image = self.image.astype('float', copy=False, casting='safe')
        self.image /= float(med)
        self.image -= 1.0

    def set_fft(self, padding):
        totalx, totaly = np.array(padding) + np.array(self.image.shape)
        w = misc.next_power_of_2(totalx)
        h = misc.next_power_of_2(totaly)
        log.debug("padding[0] %s" % int(padding[0]))
        log.debug("padding[1] %s" % int(padding[1]))
        log.debug("w, h %s %s" % (w, h))
        log.debug("totalx,y %s %s" % (totalx, totaly))
        padded_im = np.pad(self.image,
                           ((int(padding[0]), int(w-totalx)), (int(padding[1]), int(h-totaly))),
                           mode='constant')
        self.fft = np.fft.fft2(padded_im)
