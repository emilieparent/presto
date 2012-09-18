#!/usr/bin/env python

"""Inject a fake pulsar into real data, creating
a filterbank file.

Patrick Lazarus, June 26, 2012
"""
import sys
import optparse
import warnings
import shutil

import numpy as np
import scipy.integrate
import scipy.interpolate
import matplotlib.pyplot as plt

import filterbank
import psr_utils

NUMSECS = 1.0 # Number of seconds of data to use to determine global scale
              # when repacking floating-point data into integers
BLOCKSIZE = 1e4 # Number of spectra to manipulate at once


class Profile(object):
    """A class to represent a generic pulse profile.
    """
    def __init__(self, prof_func, scale=1):
        """Construct a profile.

            Inputs:
                prof_func: A function of a single variable.
                    This function should:
                        1) Represent the pulse profile.
                        2) Expect input values of phase ranging between 
                            0 and 1.
                        3) Work when provided with a numpy array.
                scale: An overall scaling factor to multiply
                    the profile by.

            Output:
                prof: The profile object.
        """
        self.prof_func = prof_func
        self.scale = scale

    def __call__(self, phs):
        """Return the value of the profile at the given phase.

            Inputs:
                phs: The phase of the profile (between 0 and 1) where
                    the profile should be evaluated.

            Output:
                vals: The values of the profile at the requested phases.
        """
        return self.scale*self.prof_func(phs)

    def plot(self, nbin=1024, scale=1):
        x0 = np.linspace(0, 1.0, nbin+1, endpoint=True)
        plt.plot(x0, self(x0)*scale)


class SplineProfile(Profile):
    def __init__(self, profvals, scale=1, **spline_kwargs):
        """Construct a profile that uses a spline to interpolate a function.

            Inputs:
                profvals: The values of the profile to be interpolated. 
                scale: An overall scaling factor to multiply
                    the profile by.
                **All additional keyword arguments are passed to the 
                    spline constructor.

            Output:
                prof: The profile object.
        """
        # TODO: Should we evaluate at the centre of the bins?
        phs = np.linspace(0,1, len(profvals)+1, endpoint=True)
        # Manually set value at phs=1.0 to the value at phs=0.0
        vals = np.concatenate((profvals, [profvals[0]]))
        # Create spline object and use it as the profile function
        spline = scipy.interpolate.InterpolatedUnivariateSpline(phs, \
                                                vals, **spline_kwargs)
        super(SplineProfile, self).__init__(spline, scale)

    def __call__(self, phs):
        """Return the value of the profile at the given phase.

            Inputs:
                phs: The phase of the profile (between 0 and 1) where
                    the profile should be evaluated.

            Output:
                vals: The values of the profile at the requested phases.
        """
        vals = super(SplineProfile, self).__call__(phs.flat)
        # Re-shape values because spline return flattened array.
        vals.shape = phs.shape
        return vals


class MultiComponentProfile(Profile):
    """A class to represent a pulse profile made up of 
        multiple components.
    """
    def __init__(self, components=None, scale=1):
        """Construct a multi-component profile.

            Input:
                components: A list of Profile objects that serve
                    as the components of this MultiComponentProfile 
                    object. (Default: Create a multi-component profile
                    with no components.)
                scale: An overall scaling factor to multiply 
                    the profile by.

            Output:
                prof: The MultiComponentProfile object.
        """
        self.scale = scale
        self.components = []
        for component in components:
            self.add_component(component)
        super(MultiComponentProfile, self).__init__(self._get_profile(), scale)

    def _get_profile(self):
        """Private method to get the pulse profile vs. phase
            function.
        """
        if self.components:
            prof = lambda ph: np.sum([comp(ph) for comp \
                                        in self.components], axis=0)
        else:
            prof = lambda ph: 0
        return prof

    def add_component(self, comp):
        self.components.append(comp)

    def plot(self, nbin=1024):
        x0 = np.linspace(0, 1.0, nbin, endpoint=False)
        plt.plot(x0, self(x0), 'k-', lw=3)
        for comp in self.components:
            comp.plot(nbin=nbin, scale=self.scale)


def get_spline_profile(prof, npts=1024, **spline_kwargs):
    """Given a profile object evaluate it and return
        a SplineProfile object. If the input profile object
        is already an instance of SplineProfile, do nothing
        and return the input profile.

        Inputs:
            prof: The profile object to conver to a SplineProfile.
            npts: The number of points to use when evaluating the
                profile. (Default: 1024)
            **All additional keyword arguments are passed to the 
                spline constructor.

        Outputs:
            spline_prof: The resulting SplineProfile object.
    """
    if isinstance(prof, SplineProfile):
        # Input profile is already a SplineProfile. Do nothing. Return it.
        return prof
    else:
        phs = np.linspace(0,1, npts, endpoint=False)
        profvals = prof(phs)/prof.scale
        spline_prof = SplineProfile(profvals, scale=prof.scale, **spline_kwargs)
        return spline_prof


def vonmises_factory(amp,shape,loc):
    # Need to use a factory for the von Mises functions
    # to make sure the lambda uses amp,shape,loc from a local
    # scope. The values in a lambda function are stored by reference
    # and only looked up dynamically when the function is called.
    def vm(ph): 
        return amp*np.exp(shape*(np.cos(2*np.pi*(ph-loc))-1))
    return vm


def create_vonmises_components(vonmises_strs):
    if not vonmises_strs:
        warnings.warn("Using default von Mises profile (Amplitude=1.0 " \
                        "b=5, and phase=0.5)")
        vonmises_comps = [Profile(vonmises_factory(1.0, 5, 0.5))]
    else:
        vonmises_comps = []
        for vonmises_str in vonmises_strs:
            split = vonmises_str.split()
            if len(split) != 3:
                raise ValueError("Bad number of von Mises components " \
                        "should be 3, got %d" % len(split))
            amp = float(split[0])
            shape = float(split[1])
            loc = float(split[2])

            # Need to use a factory for the von Mises functions
            # to make sure the lambda uses amp,shape,loc from a local
            # scope. The values in a lambda function are stored by reference
            # and only looked up dynamically when the function is called.
            vonmises_comps.append(Profile(vonmises_factory(amp,shape,loc)))
    return vonmises_comps


def inject(infile, outfn, prof, period, dm, nbitsout=None, block_size=BLOCKSIZE):
    if isinstance(infile, filterbank.FilterbankFile):
        fil = infile
    else:
        fil = filterbank.FilterbankFile(infile, read_only=True)
    print "Injecting pulsar signal into: %s" % fil.filename
    delays = psr_utils.delay_from_DM(dm, fil.frequencies)
    delays -= delays[np.argmax(fil.frequencies)]
    get_phases = lambda times: (times-delays)/period % 1

    # Create the output filterbank file
    if nbitsout is None:
        nbitsout = fil.nbits
    outfil = filterbank.create_filterbank_file(outfn, fil.header, nbits=nbitsout)

    if outfil.nbits == 8:
        # Read the first second of data to get the global scaling to use
        onesec = fil.get_timeslice(0, 1).copy()
        onesec_nspec = onesec.shape[0]
        times = np.atleast_2d(np.arange(onesec_nspec)*fil.tsamp).T+delays
        phases = times/period % 1
        onesec += prof(phases)
        minimum = np.min(onesec)
        median = np.median(onesec)
        # Set median to 1/3 of dynamic range
        global_scale = (256.0/3.0) / median
        del onesec
    else:
        # No scaling to be performed
        # These values will cause scaling to keep data unchanged
        minimum = 0
        global_scale = 1

    # Start an output file
    print "Creating out file: %s" % outfn
    sys.stdout.write(" %3.0f %%\r" % 0)
    sys.stdout.flush()
    oldprogress = -1
    
    # Loop over data
    lobin = 0
    spectra = fil.get_spectra(0, block_size)
    numread = spectra.shape[0]
    while numread:
        hibin = lobin+numread
        times = np.atleast_2d((np.arange(lobin, hibin)+0.5)*fil.tsamp).T
        phases = get_phases(times)
        toinject = prof(phases)
        injected = spectra+toinject
        scaled = (injected-minimum)*global_scale
        outfil.append_spectra(scaled)
        
        # Print progress to screen
        progress = int(100.0*hibin/fil.nspec)
        if progress > oldprogress: 
            sys.stdout.write(" %3.0f %%\r" % progress)
            sys.stdout.flush()
            oldprogress = progress
        
        # Prepare for next iteration
        lobin = hibin 
        spectra = fil.get_spectra(lobin, lobin+block_size)
        numread = spectra.shape[0]

    sys.stdout.write("Done   \n")
    sys.stdout.flush()
    

def main():
    comps = create_vonmises_components(options.vonmises)
    print "Creating profile. Number of components: %d" % len(comps)
    prof = MultiComponentProfile(comps, scale=options.scale)
    if options.use_spline:
        prof = get_spline_profile(prof)
    if options.dryrun:
        print "Showing plot of profile to be injected..."
        prof.plot()
        plt.xlim(0,1)
        plt.xlabel("Phase")
        plt.show()
        sys.exit()

    print "%d input files provided" % len(args)
    for fn in args:
        fil = filterbank.FilterbankFile(fn, read_only=True)
        outfn = options.outname % fil.header 
        inject(fil, outfn, prof, options.period, options.dm, \
                nbitsout=options.output_nbits, block_size=options.block_size)


def parse_model_file(modelfn):
    """Parse a pass model file (*.m) written by paas.
        Return a list of parameters describing each component.
        In particular (amplitude, shape, phase).

        Input:
            modelfn: The name of the model file.

        Outputs:
            params: List of parameters for each component.
                (i.e. "amplitude shape phase")
    """
    mfile = open(modelfn, 'r')
    return [" ".join(reversed(line.split())) \
                        for line in mfile.readlines()]


def parse_mfile_callback(option, opt_str, value, parser):
    vonmises = getattr(parser.values, 'vonmises')
    vonmises.extend(parse_model_file(value))


if __name__ == '__main__':
    parser = optparse.OptionParser(prog='injectpsr.py', \
                    version="v0.1 Patrick Lazarus (June 26, 2012)")
    parser.add_option("--dm", dest='dm', action='store', type='float', \
                    help="The DM of the (fake) injected pulsar signal. " \
                        "(This argument is required.", \
                    default=None)
    parser.add_option("-p", "--period", dest='period', action='store', \
                    default=None, type='float', \
                    help="The period (in seconds) of the (fake) injected " \
                        "pulsar signal. (This argument is required.)")
    parser.add_option("-s", "--scale", dest='scale', type='float', \
                    default=1, \
                    help="Overall scaling factor to multiply profile with. " \
                        "(Default: Don't scale.)")
    parser.add_option("-v", "--vonmises", dest='vonmises', action='append', \
                    help="A string of 3 parameters defining a vonmises " \
                        "component to be injected. Be sure to quote the " \
                        "3 parameters together. The params are: 'amplitude " \
                        "shape phase'. Amplitude is not related to SNR in " \
                        "any way. Also, phase should be between 0 and 1. " \
                        "(Default: if no compoments are provided " \
                        "a von Mises with amplitude=1.0, shape=5, and " \
                        "phase=0.5 will be used.)", \
                    default=[])
    parser.add_option("-m", "--model-file", dest="model_file", nargs=1, 
                    type='str', \
                    action="callback", callback=parse_mfile_callback, \
                    help="A model file (*.m) as written by 'paas'.")
    parser.add_option("--block-size", dest='block_size', default=BLOCKSIZE, \
                    type='float', \
                    help="Number of spectra per block. This is the amount " \
                        "of data manipulated/written at a time. (Default: " \
                        " %d spectra)" % BLOCKSIZE)
    parser.add_option("--nbits", dest='output_nbits', default=None, type=int, \
                    help="Number of bits per same to use in output " \
                        "filterbank file. (Default: same as input file)")
    parser.add_option("-n", "--dryrun", dest="dryrun", action="store_true", \
                    help="Show the pulse profile to be injected and exit. " \
                        "(Default: do not show profile, inject it)")
    parser.add_option("--use-spline", dest='use_spline', action='store_true', \
                    default=False, \
                    help="Evaluate the analytic pulse profile and interpolate " \
                        "with a spline. This is typically faster to execute, " \
                        "especially when the profile is made up of multiple " \
                        "components. (Default: Do not use spline.)")
    parser.add_option("-o", "--outname", dest='outname', action='store', \
                    default="injected.fil", \
                    help="The name of the output file.")
    (options, args) = parser.parse_args()
    if options.period is None or options.dm is None:
        raise ValueError("Both a period and a DM _must_ be provided!")
    main()
