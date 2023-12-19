from collections import namedtuple

import h5py
import numpy as np
from scipy.signal import convolve2d
from torch.utils.data import Dataset

from ..transforms import DataTransform, Delay, MovieTransform, Subsequence
from ..utils import recursively_load_dict_contents_from_group
from .base import (
    AttributeTransformer,
    DirectoryAttributeHandler,
    FileTreeDatasetBase,
    TransformDataset,
)


class H5SequenceSet(TransformDataset):
    def __init__(self, filename, *data_keys, output_rename=None, transforms=None, output_dict=False):
        super().__init__(transforms=transforms)

        self.output_dict = output_dict

        if output_rename is None:
            output_rename = {}

        # a flag that can be changed to turn renaming on/off
        self.rename_output = True

        self.output_rename = output_rename

        self._fid = h5py.File(filename, "r")
        self.data = self._fid
        self.data_loaded = False

        # ensure that all elements of data_keys exist
        m = None
        for key in data_keys:
            assert key in self.data, "Could not find {} in file".format(key)
            l = len(self.data[key])
            if m is not None and l != m:
                raise ValueError("groups have different length")
            m = l
        self._len = m

        # Specify which types of transforms are accepted
        self._transform_set = DataTransform

        self.data_keys = data_keys
        self.transforms = transforms or []

        self.data_point = namedtuple("DataPoint", data_keys)
        renamed_keys = [output_rename.get(k, k) for k in data_keys]
        self.output_point = namedtuple("OutputPoint", renamed_keys)

    def __dir__(self):
        attrs = set(super().__dir__())
        return attrs.union(set(self._fid.keys()))

    def load_content(self):
        self.data = recursively_load_dict_contents_from_group(self._fid)
        self.data_loaded = True

    def unload_content(self):
        self.data = self._fid
        self.data_loaded = False

    def __len__(self):
        return self._len

    def __getitem__(self, item):
        x = self.data_point(*(np.array(self.data[g][item if self.data_loaded else str(item)]) for g in self.data_keys))
        for tr in self.transforms:
            assert isinstance(tr, self._transform_set)
            x = tr(x)

        # convert to output point
        if self.rename_output:
            x = self.output_point(*x)

        if self.output_dict:
            x = x._asdict()
        return x

    def __getattr__(self, item):
        if item in self.data:
            item = self.data[item]
            if isinstance(item, h5py.Dataset):
                dtype = item.dtype
                item = item[()]
                if dtype.char == "S":  # convert bytes to unicode
                    item = item.astype(str)
                return item
            return item
        else:
            # TODO: check for a proper way to handle cases where super doesn't have __getattr__
            return super().__getattr__(item)

    def __repr__(self):
        names = ["{} -> {}".format(k, self.output_rename[k]) if k in self.output_rename else k for k in self.data_keys]
        s = "{} m={}:\n\t({})".format(self.__class__.__name__, len(self), ", ".join(names))
        if self.transforms is not None:
            s += "\n\t[Transforms: " + "->".join([repr(tr) for tr in self.transforms]) + "]"
        return s


class MovieSet(H5SequenceSet):
    """
    Extension to H5SequenceSet with specific HDF5 dataset assumed. Specifically,
    it assumes that properties such as `neurons` and `stats` are present in the dataset.
    """

    def __init__(self, filename, *data_groups, output_rename=None, transforms=None, stats_source="all"):
        super().__init__(filename, *data_groups, output_rename=output_rename, transforms=transforms)
        self.stats_source = stats_source

        # set to accept only MovieTransform
        self._transform_set = MovieTransform

    @property
    def neurons(self):
        return AttributeTransformer("neurons", self.data, self.transforms, data_group="responses")

    @property
    def n_neurons(self):
        return len(self.neurons.unit_ids)

    @property
    def input_shape(self):
        name = self.output_rename.get("inputs", "inputs") if self.rename_output else "inputs"
        return (1,) + getattr(self[0], name).shape

    def transformed_mean(self, stats_source=None):
        if stats_source is None:
            stats_source = self.stats_source

        tmp = [np.atleast_1d(self.statistics[g][stats_source]["mean"][()]) for g in self.data_keys]
        x = self.transform(self.data_point(*tmp), exclude=(Subsequence, Delay))
        if self.rename_output:
            x = self.output_point(*x)
        return x

    def rf_base(self, stats_source="all"):
        N, c, t, w, h = self.img_shape
        t = min(t, 150)
        mean = lambda dk: self.statistics[dk][stats_source]["mean"][()]
        d = dict(
            inputs=np.ones((1, c, t, w, h)) * np.array(mean("inputs")),
            eye_position=np.ones((1, t, 1)) * mean("eye_position")[None, None, :],
            behavior=np.ones((1, t, 1)) * mean("behavior")[None, None, :],
            responses=np.ones((1, t, 1)) * mean("responses")[None, None, :],
        )
        return self.transform(self.data_point(*[d[dk] for dk in self.data_keys]), exclude=Subsequence)

    def rf_noise_stim(self, m, t, stats_source="all"):
        """
        Generates a Gaussian white noise stimulus filtered with a 3x3 Gaussian filter
        for the computation of receptive fields. The mean and variance of the Gaussian
        noise are set to the mean and variance of the stimulus ensemble.
        The behvavior, eye movement statistics, and responses are set to their respective means.
        Args:
            m: number of noise samples
            t: length in time
        Returns: tuple of input, behavior, eye, and response
        """
        N, c, _, w, h = self.img_shape
        stat = lambda dk, what: self.statistics[dk][stats_source][what][()]
        mu, s = stat("inputs", "mean"), stat("inputs", "std")
        h_filt = np.float64([[1 / 16, 1 / 8, 1 / 16], [1 / 8, 1 / 4, 1 / 8], [1 / 16, 1 / 8, 1 / 16]])
        noise_input = (
            np.stack([convolve2d(np.random.randn(w, h), h_filt, mode="same") for _ in range(m * t * c)]).reshape(
                (m, c, t, w, h)
            )
            * s
            + mu
        )

        mean_beh = np.ones((m, t, 1)) * stat("behavior", "mean")[None, None, :]
        mean_eye = np.ones((m, t, 1)) * stat("eye_position", "mean")[None, None, :]
        mean_resp = np.ones((m, t, 1)) * stat("responses", "mean")[None, None, :]

        d = dict(
            inputs=noise_input.astype(np.float32),
            eye_position=mean_eye.astype(np.float32),
            behavior=mean_beh.astype(np.float32),
            responses=mean_resp.astype(np.float32),
        )

        return self.transform(
            self.data_point(*[d[dk] for dk in self.data_groups.values()]), exclude=(Subsequence, Delay)
        )


class MovieFileTreeDataset(FileTreeDatasetBase):
    _transform_types = (MovieTransform,)

    def __init__(self, *args, stats_source=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.stats_source = stats_source if stats_source is not None else "all"

    # the followings are provided for compatibility with MovieSet
    @property
    def types(self):
        return self.trial_info.types

    @property
    def tiers(self):
        return self.trial_info.tiers

    @property
    def n_neurons(self):
        target_group = "responses" if "responses" in self.data_keys else "targets"
        # check if output has been renamed
        if self.rename_output:
            target_group = self._output_rename.get(target_group, target_group)

        val = self[0]
        if hasattr(val, target_group):
            val = getattr(val, target_group)
        else:
            val = val[target_group]
        return val.shape[-1]

    @property
    def statistics(self):
        return DirectoryAttributeHandler(self.basepath / "meta/statistics", self.config["links"])

    def transformed_mean(self, stats_source=None):
        if stats_source is None:
            stats_source = self.stats_source

        tmp = [np.atleast_1d(self.statistics[g][stats_source]["mean"][()]) for g in self.data_keys]
        x = self.transform(self.data_point(*tmp), exclude=(Subsequence, Delay))
        if self.rename_output:
            x = self._output_point(*x)
        return x


class NRandomSubSequenceDataset(Dataset):
    """
    Data augmentation for training.
    Generate a new dataset based on original_dat, by random sampling of each training item in original_dat for multiple times.
    This only works for movie data and each sampling is a subsequence of the full sequence in a original_dat item.
    Args:
        original_dat: an original dataset
        num_random_subsequence: number of subsequences sampled from each original_dat item
        subsequence_length: the length of each subsequence
        sequence_length: full sequence length of a training item from original_dat
        random_start: array, start positions at each original_dat item for random sampling
        seed: random seed
    """

    def __init__(
        self,
        original_dat,
        num_random_subsequence=10,
        subsequence_length=100,
        sequence_length=300,
        random_start=None,
        seed=10,
    ):
        new_tiers = []  # list, tiers for each item in new dataset
        new_inds = []  # list, indice for each item in new dataset
        for ii, tier in enumerate(original_dat.trial_info.tiers):
            if tier != "none":  # if tier!='none' and ii<15:
                # print (ii, dat2[ii]._fields, tier)
                if tier == "train":
                    new_tiers.extend(["train"] * num_random_subsequence)
                    new_inds.extend([ii] * num_random_subsequence)
                else:
                    new_tiers.append(tier)
                    new_inds.append(ii)

        self.original_dat = original_dat
        self.new_tiers = new_tiers
        self.new_inds = new_inds
        if random_start is None:
            np.random.seed(seed)
            self.random_start = np.random.randint(
                low=0, high=sequence_length - subsequence_length, size=num_random_subsequence
            )
        else:  # manually specify the start positions
            assert (
                len(random_start) == num_random_subsequence
            ), f"Number of start positions {len(random_start)} != number of subsequence {num_random_subsequence}"
            self.random_start = random_start
        self.num4rand = len(self.random_start)
        self.random_end = self.random_start + subsequence_length

    def __getitem__(self, index):
        if self.new_tiers[index] == "train":
            return self.original_dat[self.new_inds[index]].__class__(
                **{
                    k: getattr(self.original_dat[self.new_inds[index]], k)[
                        :,
                        self.random_start[index % self.num4rand] : self.random_end[index % self.num4rand],
                    ]
                    for k in self.original_dat[self.new_inds[index]]._fields
                }
            )

        else:
            return self.original_dat[self.new_inds[index]]

    def __len__(self):
        return len(self.new_tiers)

    @property
    def neurons(self):
        return self.original_dat.neurons
