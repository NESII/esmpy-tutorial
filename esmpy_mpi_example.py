#!/usr/bin/env python

"""
This is an example script showing how to do parallel regridding with ESMPy. ESMPy abstracts some
of the parallel components from the user so that very few calls to mpi4py methods are necessary.
"""

import ESMF
import matplotlib.pyplot as plt
from mpi4py import MPI
import numpy as np

__author__ = "Nathan Wendt"
__copyright__ = "Copyright 2017"
__email__ = "nathan.wendt@noaa.gov"
__license__ = "MIT"
__maintainer__ = "Nathan Wendt"
__version__ = "1.0.1"

############################################# CONFIG #############################################
# This is the variable that will be interpolated. A few others are possible.
IVAR = 'dpc'

# This will  toggle the use of the `Gatherv` method. The `Gatherv` method can
# send unequal chunks of numpy arrays to other MPI processes. This will 
# **generally** be faster than the `gather` method that sends python objects.
# From my limited experience, `Gatherv` is faster for larger grids, but not grids
# that are as small as being used in this example. As always, test for yourself
# on your system to determine the best choice for your particular use case.
GATHERV = True

# Toggle some informative print statements
VERBOSE = True

# Toggle plot, saves otherwise
PLOT = True

# Below, you will see arrays that are transposed. This is done to get the arrays into Fortran
# contiguous memory order. ESMF subroutines that are called are written in Fortran and passing
# arrays with their native memory order will help improve efficiency. Unfortunately, these
# changes can make the logic of the script less intuitive. The efficiency gain will mostly affect
# larger grid sizes. The good news is that you can remove the the transpose (`.T`) calls from the
# arrays and go back to the original, straightforward code without a problem.
FORTRAN_CONTIGUOUS = True
##################################################################################################

def get_processor_bounds(target, staggerloc):
    """
    :param target: The grid object from which to extract local bounds.
    :type target: :class:`ESMF.Grid`
    :return: A tuple of integer bounds. See ``return`` statement.
    :rtype: tuple
    """

    # The lower_bounds and upper_bounds properties give us global indices of the processor local
    # bounds. The assumed dimension order is Z, Y, X (based on the data being used in this example)

    x_lower_bound = target.lower_bounds[staggerloc][1]
    x_upper_bound = target.upper_bounds[staggerloc][1]
    y_lower_bound = target.lower_bounds[staggerloc][0]
    y_upper_bound = target.upper_bounds[staggerloc][0]

    return x_lower_bound, x_upper_bound, y_lower_bound, y_upper_bound

# Turn on the debugger. An output file for each process will be produced
ESMF.Manager(debug=True)

# Set up MPI communicator and get environment information
comm = MPI.COMM_WORLD
rank = comm.rank
size = comm.size

if rank == 0 and VERBOSE:
    print('Loading data...')

#RUC Grid (this will be the source grid)
with np.load('ruc2_130_20120414_1200_006.npz') as ruc:
    if FORTRAN_CONTIGUOUS:
        dat = ruc[IVAR].T
        rlat = ruc['lat'].T
        rlon = ruc['lon'].T
    else:
        dat = ruc[IVAR]
        rlat = ruc['lat']
        rlon = ruc['lon']

# NAM grid (this will be the destination grid)
with np.load('nam_218_20120414_1200_006.npz') as nam:
    if FORTRAN_CONTIGUOUS:
        nlat = nam['lat'].T
        nlon = nam['lon'].T
    else:
        nlat = nam['lat']
        nlon = nam['lon']

if GATHERV:
    # When using the `Gatherv` method the final output will be gathered on the root 
    # process (rank = 0 in this case). It is faster to call `numpy.empty` vs.
    # `numpy.zeros`. We know we will fill it with data in the end so the random input
    # bits should not matter here. All other processes should also have the variable
    # defined as the None object.
    if rank == 0:
        final = np.empty(nlat.shape)
    else:
        final = None

# Set up the source grid
if rank == 0 and VERBOSE:
    print('Source grid setup...')
sourcegrid = ESMF.Grid(np.asarray(rlat.shape), coord_sys=ESMF.CoordSys.SPH_DEG,
                       staggerloc=ESMF.StaggerLoc.CENTER)
slat = sourcegrid.get_coords(1)
slon = sourcegrid.get_coords(0)

# The bounds are critical when doing parallel regridding. ESMPy abstracts much of the MPI
# environment setup from the user. Perhaps the best part of that abstraction is the ability
# to not worry about scattering the data amongst the processes (load balancing is *not* done).
# ESMPy will split your work up for you (along dimension 0). The bounds are then stored within the
#`Grid` object on each spawned process. These bounds will have to be used to subset all coordinate
# and data movement for the script (this includes the mask as well). Given how the bounds work, you
# can still run this script in serial mode and still be able to regrid your data.
x_lower_bound, x_upper_bound, y_lower_bound, y_upper_bound = get_processor_bounds(sourcegrid, ESMF.StaggerLoc.CENTER)

# We can see exactly how ESMPy has split the grid up here
if VERBOSE:
    # Make sure the printing is together for all  processes
    comm.Barrier()
    print('Process Rank {} :: Bounds {}'.format(rank, get_processor_bounds(sourcegrid, ESMF.StaggerLoc.CENTER)))

# Input the coordinates into the source grid. Recall our dimension order of ZYX.
slat[...] = rlat[y_lower_bound:y_upper_bound, x_lower_bound:x_upper_bound]
slon[...] = rlon[y_lower_bound:y_upper_bound, x_lower_bound:x_upper_bound]

# Prepare the source field and input the data
sourcefield = ESMF.Field(sourcegrid, name='Native RUC 2 m Dewpoint')
sourcefield.data[...] = dat[y_lower_bound:y_upper_bound, x_lower_bound:x_upper_bound]

# Set up the destination grid
if rank == 0 and VERBOSE:
    print('Destination grid setup...')
destgrid = ESMF.Grid(np.asarray(nlat.shape),
                     coord_sys=ESMF.CoordSys.SPH_DEG,
                     staggerloc=ESMF.StaggerLoc.CENTER)
dlat = destgrid.get_coords(1)
dlon = destgrid.get_coords(0)

# Get the bounds for the destination grid. For simplicity, I have just overwritten the values
# grabbed from the source grid.
x_lower_bound, x_upper_bound, y_lower_bound, y_upper_bound = get_processor_bounds(destgrid, ESMF.StaggerLoc.CENTER)

# Add the coordinates to the destination grid
dlat[...] = nlat[y_lower_bound:y_upper_bound, x_lower_bound:x_upper_bound]
dlon[...] = nlon[y_lower_bound:y_upper_bound, x_lower_bound:x_upper_bound]

# Set up the destination field
destfield = ESMF.Field(destgrid, name='Interpolated NAM 2 m Dewpoint')

# Set up the `Regrid` object. I give it the value from conus_mask that is considered a masked
# location (here 0). This is when the regridding weights will be generated by each MPI process.
if rank == 0 and VERBOSE:
    print('Calculating regridding weights...')
regrid = ESMF.Regrid(sourcefield, destfield, regrid_method=ESMF.RegridMethod.BILINEAR,
                     unmapped_action=ESMF.UnmappedAction.IGNORE)

# Do the actual regrid
if rank == 0 and VERBOSE:
    print('Regridding...')
destfield = regrid(sourcefield, destfield)

# Recompose the domain. While working in parallel, you have to be aware that each process will
# have a small portion of your domain to regrid. If you need to do some work with the full,
# finalized grid, you will have to recombine the pieces. There are two options that have worked
# well for me:
#
# 1) `gather` method
# 2) `Gatherv` method
#
# The `gather` method is much more straightforward than the `Gatherv` method; however, `Gatherv`
# is likely to be faster for larger grids. This example defaults to using the `gather` method.
if rank == 0 and VERBOSE:
    print('Aggregating data...')
if GATHERV:
    # To use `Gatherv` you need to know how much data will be sent in the buffer as well as its
    # displacement (which tells MPI where each piece will end up in the numpy array). Using the
    # bounds information (which is from the destination grid here) and the shape of the destination
    # we can calculate it. 
    sendcounts, displacements = np.multiply(*destfield.data.shape), (x_upper_bound - x_lower_bound) * y_lower_bound
    if VERBOSE:
        if rank == 0:
            print('**Using Gatherv method**')
        comm.Barrier()
        print('Process Rank {} :: sendcounts {} displacement {}'.format(rank, sendcounts, displacements))

    # Since the root process will be gathering from the child processes and reassembling the array
    # we need to send all the counts and displacements to the root process. Note here that the data
    # will be placed in a list/array in rank order automatically, which is what we want.
    sendcounts = comm.gather(sendcounts, root=0)
    displacements = comm.gather(displacements, root=0)

    # Using `Gatherv` we can send the destination field data to the final array on the root process
    # and place the data in the right location. One quirk of this approach is the need to call the
    # `numpy.ascontiguousarray` function on the data being sent. Without doing this the data coming
    # out of ESMPy will be out of order (Fortran, column-major) for what `Gatherv` is expecting
    # (C, row-major), leading to an awkwardly striped array.
    comm.Gatherv(np.ascontiguousarray(destfield.data), [final, sendcounts, displacements, MPI.DOUBLE], root=0)
else:
    if rank == 0 and VERBOSE:
        print('**Using gather method**')
    # This is the simpler `gather` method that sends the array as a python object. There is no need
    # for counts or displacements.
    final = comm.gather(destfield.data, root=0)
    if rank == 0:
        # This method does not place all the data in the same array, but places each piece
        # in a list/array, again, in rank order. With that list/array, we can concatenate the
        # pieces back together to create the full array again.
        final = np.concatenate([final[i] for i in range(size)], axis=0)

if rank ==  0:
    if PLOT:
        if VERBOSE:
            print('Plotting...')
        # Very crude plot to see results. In the plot you will notice that the data have a
        # "box" of zeros around them. That is because we have interpolated the smaller
        # RUC grid extent to the larger NAM grid extent. No extrapolation is done where
        # points are not mapped so they remain zeros by default.
        if FORTRAN_CONTIGUOUS:
            plt.pcolormesh(final.T)
        else:
            plt.pcolormesh(final)
        plt.colorbar()
        plt.show()
    else:
        if VERBOSE:
            print('Saving...')
        if FORTRAN_CONTIGUOUS:
            np.savez_compressed('esmpy_mpi_regrid', dat=final.T)
        else:
            np.savez_compressed('esmpy_mpi_regrid', dat=final)
