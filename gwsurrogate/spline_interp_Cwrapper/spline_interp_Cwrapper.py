import ctypes
from ctypes import c_double, c_long, POINTER
import numpy as np
import os

def _load_spline_interp(dll_path,function_name):
    dll = ctypes.CDLL(dll_path, mode=ctypes.RTLD_GLOBAL)
    func = dll.spline_interp
    func.argtypes = [c_long, c_long,
        POINTER(c_double), POINTER(c_double),
        POINTER(c_double), POINTER(c_double)]
    return func

dll_dir = os.path.dirname(os.path.realpath(__file__))
c_interp = _load_spline_interp('%s/_spline_interp.so'%dll_dir, 'spline_interp')

def interpolate(xnew, x, y):

    x = x.astype('float64')
    y = y.astype('float64')
    xnew = xnew.astype('float64')

    x_p = x.ctypes.data_as(POINTER(c_double))
    y_p = y.ctypes.data_as(POINTER(c_double))
    xnew_p = xnew.ctypes.data_as(POINTER(c_double))

    ynew  = np.zeros(xnew.shape[0])
    ynew_p = ynew.ctypes.data_as(POINTER(c_double))

    c_interp(x.shape[0],xnew.shape[0],x_p,y_p,xnew_p,ynew_p)

    return ynew
