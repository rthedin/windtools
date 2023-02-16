import pandas as pd
import xarray as xr
import numpy as np
import os
from netCDF4 import Dataset

class ABLStatistics(object):

    def __init__(self,fpath,start_date=None,mean_profiles=False):
        self.fpath = fpath
        if start_date:
            self.datetime0 = pd.to_datetime(start_date)
        else:
            self.datetime0 = None
        self._load_timeseries()
        if mean_profiles:
            self._load_timeheight_profiles()

    def _setup_time_coords(self,ds):
        if self.datetime0:
            dt = self.datetime0 + pd.to_timedelta(ds['time'], unit='s')
            ds = ds.assign_coords({'datetime':('num_time_steps',dt)})
            ds = ds.swap_dims({'num_time_steps':'datetime'})
            self.time_coord = 'datetime'
        else:
            ds = ds.swap_dims({'num_time_steps':'time'})
            self.time_coord = 'time'
        return ds

    def _load_timeseries(self):
        ds = xr.load_dataset(self.fpath)
        ds = self._setup_time_coords(ds)
        self.ds = ds

    def _load_timeheight_profiles(self):
        ds = xr.load_dataset(self.fpath, group='mean_profiles')
        ds = ds.rename({'h':'height'})
        times = self.ds.coords[self.time_coord].values
        ds = ds.assign_coords({
            self.time_coord: ('num_time_steps',times),
            'height': ds['height'],
        })
        ds = ds.swap_dims({'num_time_steps':self.time_coord, 'nlevels':'height'})
        ds = ds.transpose(self.time_coord,'height')
        self.ds = xr.combine_by_coords([self.ds, ds])
        self.ds[self.time_coord] = np.round(self.ds[self.time_coord],5)





class Sampling(object):

    def __init__(self,fpath,start_date=None,read_data=False):
        self.fpath = fpath
        self.dt    = None

        self.groups = self._get_groups()
        
        if read_data:
            self.read_data(groups_to_read = self.groups)

    def __repr__(self):
        header=f'Following groups sampled:\n'
        return header+''.join([f'    {g}\n' for g in self.groups])


    def getGroupProperties(self, ds=None, group=None):

        if ds is None and group is None:
            raise ValueError(f'Either `ds` or `group` must be specified')

        if ds is None and group is not None:
            ds = xr.open_dataset(self.fpath, group=group, engine='netcdf4')

        self.sampling_type = ds.sampling_type

        [self.nx, self.ny, self.nz] = ds.ijk_dims
        self.ndt = len(ds.num_time_steps)
        self.tdi = ds.num_time_steps[0]
        self.tdf = ds.num_time_steps[-1]

        # identify the normal
        if ds.axis3[0] == 1: self.normal='x'
        if ds.axis3[1] == 1: self.normal='y'
        if ds.axis3[2] == 1: self.normal='z'

        # Get axes
        self.x = np.sort(np.unique(ds['coordinates'].isel(ndim=0)))
        self.y = np.sort(np.unique(ds['coordinates'].isel(ndim=1)))
        self.z = np.sort(np.unique(ds['coordinates'].isel(ndim=2)))


    def _get_groups(self):
        with Dataset(self.fpath) as f:
            groups = list(f.groups.keys())
        groups = [groups] if isinstance(groups,str) else groups
        return groups


    def set_dt(self, dt):
        self.dt = dt

    def read_data(self, groups_to_read):

        groups_to_read = groups_to_read if isinstance(groups_to_read,str) else groups_to_read

        ds_all = []
        for g in groups_to_read:
            ds_single = read_single_group(g)
            ds_all.append(ds_single)

        return ds_all


    def read_single_group(self, group, itime=0, ftime=-1, step=1, outputPath=None, var=['u','v','w'], simCompleted=False, verbose=False):
        '''

        step: int
            Get the output at every step steps. For instance, if a sampling needs to be done
            at 2 s, and other at 0.5 s, then save everything at 0.5 s and when reading the group 
            related to the 2-s sampling, set step to 4
        outputPath: str (default:None)
            If different than None, it is the directory where intermediate and final (concatenated)
            files will be saved.
        var: str, list of str
            variables to be outputted. By defaul, u, v, w. If temperature and tke are available,
            use either var='all' or var=['u','v','w','tke'], for example.
        simCompleted: bool, default False
            If the simulation is still running, the nc file needs to be open using `load_dataset`. 
            This function does _not_ load the data lazily, so it is prohibitively expensive to use
            it in large cases. The function `open_dataset` does load the data lazily, however, it
            breaks any currently running simulation as it leaves the file open for reading, clashing
            with the code that has it open for writing. If the simulation is done, the file is no
            longer open for writing and can be opened for reading using lazy `open_dataset`. This
            bool variable ensures that you _explicitly_ state the simulation is done, so that you
            don't crash something by mistake. simCompleted set to true will likely need to come with
            the specification of nchunks>1, since memory is likely an issue in these cases.

        '''
        
        if simCompleted:
            dsraw = xr.open_dataset(self.fpath, group=group, engine='netcdf4')
        else:
            dsraw = xr.load_dataset(self.fpath, group=group, engine='netcdf4')


        if isinstance(var,str): var = [var]
        if var==['all']:
            self.reqvars = ['u','v','w','temperature','tke']
        else:
            self.reqvars = var


        self.getGroupProperties(ds = dsraw)

        if   self.sampling_type == 'LineSampler':
            ds = self._read_line_sampler(dsraw)
        elif self.sampling_type == 'LidarSampler':
            ds = self._read_lidar_sampler(dsraw)
        elif self.sampling_type == 'PlaneSampler':
            ds = self._read_plane_sampler(dsraw, group, itime, ftime, step, outputPath, verbose)
        elif self.sampling_type == 'ProbeSampler':
            ds = self._read_probe_sampler(dsraw)
        else:
            raise ValueError(f'Stopping. Sampling type {self.sampling_type} not recognized')

        return ds


    def _read_line_sampler(self,ds):
        raise NotImplementedError(f'Sampling `LineSampler` is not implemented. Consider implementing it..')

    def _read_lidar_sampler(self,ds):
        raise NotImplementedError(f'Sampling `LidarSampler` is not implemented. Consider implementing it.')

    def _read_plane_sampler(self, ds, group, itime, ftime, step, outputPath, verbose):

        if ftime == -1:
            ftime = self.ndt

        # Unformatted arrays
        velx_old_all = ds['velocityx'].isel(num_time_steps=slice(itime, ftime, step)).values
        vely_old_all = ds['velocityy'].isel(num_time_steps=slice(itime, ftime, step)).values
        velz_old_all = ds['velocityz'].isel(num_time_steps=slice(itime, ftime, step)).values
 
        # Number of time steps 
        ndt = len(ds['velocityx'].isel(num_time_steps=slice(itime,ftime,step)).num_time_steps)

        velx_all = np.reshape(velx_old_all, (ndt, self.nz, self.ny, self.nx)).T
        vely_all = np.reshape(vely_old_all, (ndt, self.nz, self.ny, self.nx)).T
        velz_all = np.reshape(velz_old_all, (ndt, self.nz, self.ny, self.nx)).T

        # The order of the dimensions varies depending on the `normal`
        if   (ds.axis3 == [1,0,0]).all(): ordereddims = ['y','z','x','samplingtimestep']
        elif (ds.axis3 == [0,1,0]).all(): ordereddims = ['x','z','y','samplingtimestep']
        elif (ds.axis3 == [0,0,1]).all(): ordereddims = ['x','y','z','samplingtimestep']
        else: raise ValueError('Unknown normal plane')

        new_all = xr.DataArray(data = velx_all, 
                       dims = ordereddims,
                       coords=dict(
                           x=('x',self.x),
                           y=('y',self.y),  
                           z=('z',self.z),
                           samplingtimestep=('samplingtimestep',range(itime, ftime, step)),
                       )
                      )
        new_all = new_all.to_dataset(name='u')
        new_all['v'] = (ordereddims, vely_all)
        new_all['w'] = (ordereddims, velz_all)

        if 'temperature' in list(ds.keys()) and 'temperature' in self.reqvars:
            temp_old_all = ds['temperature'].isel(num_time_steps=slice(itime, ftime, step)).values
            temp_all = np.reshape(temp_old_all, (ndt, self.nz, self.ny, self.nx)).T
            new_all['temperature'] = (ordereddims, temp_all)

        if 'tke' in list(ds.keys()) and 'tke' in self.reqvars:
            tke_old_all = ds['tke'].isel(num_time_steps=slice(itime, ftime, step)).values
            tke_all = np.reshape(tke_old_all, (ndt, self.nz, self.ny, self.nx)).T
            new_all['tke'] = (ordereddims, tke_all)

        if outputPath is not None:
            if outputPath.endswith('.zarr'):
                print(f'Saving {outputPath}')
                new_all.to_zarr(outputPath)
            elif outputPath.endswith('.nc.'):
                print(f'Saving {outputPath}')
                new_all.to_netcdf(outputPath)
            else:
                print(f'Saving {group}.zarr')
                new_all.to_zarr(os.path.join(outputPath,f'{group}.zarr'))

        return new_all


    def _read_probe_sampler(self,ds):
        raise NotImplementedError(f'Sampling `ProbeSampler` is not implemented. Consider implementing it.')



    def to_vtk(self, dsOrGroup, outputPath, verbose=True, offsetz=0, itime_i=0, itime_f=-1):
        '''
        Writes VTKs for all time stamps present in ds

        dsOrGroup: DataSet or str
            If given a dataset (obtained before with `read_single_group`) then it uses that. If string
            is given, then calls the `read_single_group` before proceeding.
        outputPath: str
            Path where the VTKs should be saved. Should exist. This is useful when specifying 'Low' and
            'HighT*' high-level directories. outputPath = os.path.join(path,'processedData','HighT1')
        itime_i, itime_f: int
            Initial and final index for time if only a subset of the dataset is desired

        '''
        if not os.path.exists(outputPath):
            raise ValueError(f'The output path should exist. Stopping.')

        if isinstance(dsOrGroup,xr.Dataset):
            ds = dsOrGroup
            ndt = len(ds.samplingtimestep)
            xarray = ds.x
            yarray = ds.y
            zarray = ds.z
        else:
            ds = self.read_single_group(dsOrGroup)
            ndt = len(ds.num_time_steps)
            xarray = self.x
            yarray = self.y
            zarray = self.z

        if itime_f==-1:
            itime_f = ndt


        for t in np.arange(itime_i, itime_f):

            dstime = ds.isel(samplingtimestep=t)
            currentvtk = os.path.join(outputPath,f'Amb.t{t}.vtk')

            if verbose:
                print(f'Saving {currentvtk}')

            with open(currentvtk,'w', encoding='utf-8') as vtk:
                vtk.write(f'# vtk DataFile Version 3.0\n')
                vtk.write(f'{self.sampling_type} with offset in z of {offsetz}\n')
                vtk.write(f'ASCII\n')
                vtk.write(f'DATASET STRUCTURED_POINTS\n')
                vtk.write(f'DIMENSIONS {self.nx} {self.ny} {self.nz}\n')
                vtk.write(f'ORIGIN {self.x[0]} {self.y[0]} {self.z[0]+offsetz}\n')
                vtk.write(f'SPACING {self.x[1]-self.x[0]} {self.y[1]-self.y[0]} {self.z[1]-self.z[0]}\n')
                vtk.write(f'POINT_DATA {self.nx*self.ny*self.nz}\n')
                vtk.write(f'FIELD attributes 1\n')
                vtk.write(f'U 3 {self.nx*self.ny*self.nz} float\n')
                for z in zarray:
                    for y in yarray:
                        for x in xarray:
                            point = dstime.sel(x=x,y=y,z=z)
                            vtk.write(f'{point.u.values:.5f}\t{point.v.values:.5f}\t{point.w.values:.5f}\n')
            


def addDatetime(ds,dt,origin=pd.to_datetime('2000-01-01 00:00:00'), computemean=True):
    
    if dt <= 0:
        raise ValueError(f'The dt should be positive. Received {dt}.')

    # Add time array
    ds['time'] = (('samplingtimestep'), ds['samplingtimestep'].values*dt)

    # Save original sampling time step array
    samplingtimestep = ds['samplingtimestep'].values

    # Rename and add datetime information
    ds = ds.rename({'samplingtimestep':'datetime'})
    ds = ds.assign_coords({'datetime':pd.to_datetime(ds['time'], unit='s', origin=origin)})

    # Add back the original sampling time step
    ds['samplingtimestep'] = (('datetime'), samplingtimestep)

    if computemean:
        # Compute or grab means (mean will be available if chunked saving)
        if 'umean' in ds.keys():
            meanu = ds['umean']
            meanv = ds['vmean']
            meanw = ds['wmean']
            ds = ds.drop_vars(['umean','vmean','wmean'])
        else:
            meanu = ds['u'].mean(dim='datetime')
            meanv = ds['v'].mean(dim='datetime')
            meanw = ds['w'].mean(dim='datetime')

        # Add mean computations
        ds['up'] = ds['u'] - meanu
        ds['vp'] = ds['v'] - meanv
        ds['wp'] = ds['w'] - meanw

    return ds







