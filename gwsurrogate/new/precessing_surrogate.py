"""
A module for evaluating precessing surrogate models of gravitational waves
from numerical relativity simulations of binary black hole mergers.
"""

import os
import numpy as np
import h5py
from scipy.interpolate import InterpolatedUnivariateSpline as spline
from gwsurrogate.precessing_utils import _utils
import warnings
from gwtools.harmonics import sYlm
from gwsurrogate import spline_interp_Cwrapper


###############################################################################
# Simple quaternion functions

def multiplyQuats(q1, q2):
    return np.array([
            q1[0]*q2[0] - q1[1]*q2[1] - q1[2]*q2[2] - q1[3]*q2[3],
            q1[2]*q2[3] - q2[2]*q1[3] + q1[0]*q2[1] + q2[0]*q1[1],
            q1[3]*q2[1] - q2[3]*q1[1] + q1[0]*q2[2] + q2[0]*q1[2],
            q1[1]*q2[2] - q2[1]*q1[2] + q1[0]*q2[3] + q2[0]*q1[3]])

def quatInv(q):
    """Returns QBar such that Q*QBar = 1"""
    qConj = -q
    qConj[0] = -qConj[0]
    normSqr = multiplyQuats(q, qConj)[0]
    return qConj/normSqr

###############################################################################
# Functions related to frame transformations

def _assemble_powers(thing, powers):
    return np.array([thing**power for power in powers])

def _wignerD_matrices(q, ellMax):
    """
Given a quaternion q with shape (4, N) and some maximum ell value ellMax,
computes W[ell, m', m](t_i) for i=0, ..., N-1, for 2 \leq ell \leq ellMax,
for -L \leq m', m \leq L.
Returns a list where each entry is a numpy array with shape
((2*ell+1), (2*ell+1), N) corresponding to a given value of ell, taking indices
for m', m, and t_i.

Parts of this function are adapted from GWFrames:
https://github.com/moble/GWFrames
written by Michael Boyle, based on his paper:
http://arxiv.org/abs/1302.2919
    """
    ra = q[0] + 1.j*q[3]
    rb = q[2] + 1.j*q[1]
    ra_small = (abs(ra) < 1.e-12)
    rb_small = (abs(rb) < 1.e-12)
    i1 = np.where((1 - ra_small)*(1 - rb_small))[0]
    i2 = np.where(ra_small)[0]
    i3 = np.where((1 - ra_small)*rb_small)[0]

    n = len(ra)
    lvals = range(2, ellMax+1)
    matrices = [0.j*np.zeros((2*ell+1, 2*ell+1, n)) for ell in lvals]

    # Determine res at i2: it's 0 unless mp == -m
    # Determine res at i3: it's 0 unless mp == m
    for i, ell in enumerate(lvals):
        for m in range(-ell, ell+1):
            if (ell+m)%2 == 1:
                matrices[i][ell+m, ell-m, i2] = rb[i2]**(2*m)
            else:
                matrices[i][ell+m, ell-m, i2] = -1*rb[i2]**(2*m)
            matrices[i][ell+m, ell+m, i3] = ra[i3]**(2*m)

    # Determine res at i1, where we can safely divide by ra and rb
    ra = ra[i1]
    rb = rb[i1]
    ra_pows = _assemble_powers(ra, range(-2*ellMax, 2*ellMax+1))
    rb_pows = _assemble_powers(rb, range(-2*ellMax, 2*ellMax+1))
    abs_raSqr_pows = _assemble_powers(abs(ra)**2, range(0, 2*ellMax+1))
    absRRatioSquared = (abs(rb)/abs(ra))**2
    ratio_pows = _assemble_powers(absRRatioSquared, range(0, 2*ellMax+1))

    for i, ell in enumerate(lvals):
        for m in range(-ell, ell+1):
            for mp in range(-ell, ell+1):
                factor = _utils.wigner_coef(ell, mp, m)
                factor *= ra_pows[2*ellMax + m+mp]
                factor *= rb_pows[2*ellMax + m-mp]
                factor *= abs_raSqr_pows[ell-m]
                rhoMin = max(0, mp-m)
                rhoMax = min(ell+mp, ell-m)
                s = 0.
                for rho in range(rhoMin, rhoMax+1):
                    c = ((-1)**rho)*(_utils.binom(ell+mp, rho)*
                                     _utils.binom(ell-mp, ell-rho-m))
                    s += c * ratio_pows[rho]
                matrices[i][ell+m, ell+mp, i1] = factor*s

    return matrices

def rotateWaveform(quat, h):
    """
Transforms a waveform from the coprecessing frame to the inertial frame.
quat: A quaternion array with shape (4, N) where N is the number of time
      samples describing the coprecessing frame
h: An array of waveform modes with shape (n_modes, N). The modes are ordered
    (2, -2), ..., (2, 2), (3, -3), ...
    and n_modes = 5, 12, or 21 for ellMax = 2, 3, or 4.

Returns: h_inertial, a similar array to h containing the inertial frame modes.
    """
    quat = quatInv(quat)

    ellMax = {
            5: 2,
            12: 3,
            21: 4,
            32: 5,
            45: 6,
            60: 7,
            77: 8,
            }[len(h)]

    matrices = _wignerD_matrices(quat, ellMax)

    res = 0.*h
    i=0
    for ell in range(2, ellMax+1):
        for m in range(-ell, ell+1):
            for mp in range(-ell, ell+1):
                res[i+m+ell] += matrices[ell-2][ell+m, ell+mp]*h[i+mp+ell]
        i += 2*ell + 1
    return res

def transformTimeDependentVector(quat, vec):
    """
Given a coprecessing frame quaternion quat, with shape (4, N),
and a vector vec, with shape (3, N), transforms vec from the
coprecessing frame to the inertial frame.
    """
    qInv = quatInv(quat)
    return multiplyQuats(quat, multiplyQuats(np.append(np.array([
            np.zeros(len(vec[0]))]), vec, 0), qInv))[1:]


###############################################################################
# Functions related to fit evaluations

def _get_fit_settings():
    """
     These are to rescale the mass ratio fit range
     from [-0.01, np.log(4+0.01)] to [-1, 1]. The chi fits are already in
     this range.
    """
    q_fit_offset = -0.9857019407834238
    q_fit_slope = 1.4298059216576398
    q_max_bfOrder = 4
    chi_max_bfOrder = 2
    return q_fit_offset, q_fit_slope, q_max_bfOrder, chi_max_bfOrder

def _get_fit_params(x):
    """ Converts from x=[q, chi1x, chi1y, chi1z, chi2x, chi2y, chi2z]
        to x = [np.log(q), chi1x, chi1y, chiHat, chi2x, chi2y, chi_a]
        chiHat is defined in Eq.(3) of 1508.07253.
        chi_a = (chi1 - chi2)/2.
        Both chiHat and chi_a always lie in range [-1, 1].
    """

    x = np.copy(x)

    q = float(x[0])
    chi1z = float(x[3])
    chi2z = float(x[6])
    eta = q/(1.+q)**2
    chi_wtAvg = (q*chi1z+chi2z)/(1+q)
    chiHat = (chi_wtAvg - 38.*eta/113.*(chi1z + chi2z)) \
        /(1. - 76.*eta/113.)
    chi_a = (chi1z - chi2z)/2.

    x[0] = np.log(q)
    x[3] = chiHat
    x[6] = chi_a

    return x

def _eval_scalar_fit(fit_data, fit_params):
    """ Evaluates a single scalar fit.
        fit_params should come from _get_fit_params()
    """
    q_fit_offset, q_fit_slope, q_max_bfOrder, chi_max_bfOrder \
        = _get_fit_settings()
    val = _utils.eval_fit(fit_data['bfOrders'], fit_data['coefs'], \
        fit_params, q_fit_offset, q_fit_slope, q_max_bfOrder, chi_max_bfOrder)
    return val

def _eval_vector_fit(fit_data, size, fit_params):
    """ Evaluates a vector fit, where each element is a scalar fit.
        fit_params should come from _get_fit_params()
    """
    val = []
    for i in range(size):
        val.append(_eval_scalar_fit(fit_data[i], fit_params))
    return np.array(val)

###############################################################################

class DynamicsSurrogate:
    """
A surrogate intended to reproduce the orbital, precession, and spin dynamics
of numerical relativity BBH waveforms and spins.

This surrogate models:
    -The coprecessing frame
    -The orbital phase in the coprecessing frame, which we can use
     to find the coorbital frame
    -The spin directions in the coprecessing frame

As input, it takes:
    -The quaternion giving the initial coprecessing frame
    -The initial orbital phase in the coprecessing frame
    -The initial spin directions in the coprecessing frame

Using the input, it evolves a system of ODEs.
Given the things this surrogate models at time t, it evaluates a
prediction for:
    -The quaternion derivative in the coprecessing frame: q^{-1}(t)q'(t)
     from which q'(t) is computed
    -The orbital frequency in the coprecessing frame
    -The time derivatives of the spins in the coprecessing frame
These time derivatives are given to the AB4 ODE solver.
    """

    def __init__(self, h5file):
        """h5file is a h5py.File containing the surrogate data"""
        self.t = h5file['t_ds'].value


        self.fit_data = []
        for i in range(len(self.t)):
            group = h5file['ds_node_%s'%(i)]
            tmp_data = {}

            tmp_data['omega'] = self._load_scalar_fit(group, 'omega')

            tmp_data['omega_orb'] =self._load_vector_fit(group, 'omega_orb', 2)
            tmp_data['chiA'] =self._load_vector_fit(group, 'chiA', 3)
            tmp_data['chiB'] =self._load_vector_fit(group, 'chiB', 3)

            self.fit_data.append(tmp_data)

        self.diff_t = np.diff(self.t)
        self.L = len(self.t)

        # Validate time array
        for i in range(3):
            if not self.diff_t[2*i] == self.diff_t[2*i+1]:
                raise Exception("ab4 needs to do 3 steps of RK4 integration!")

    def _load_scalar_fit(self, group, key):
        """ Loads a single scalar fit """
        fit_data = {
                'coefs': group['%s_coefs'%(key)].value,
                'bfOrders': group['%s_bfOrders'%(key)].value
                }
        return fit_data

    def _load_vector_fit(self, group, key, size):
        """ Loads a vector fit, where each element is a scalar fit """
        fit_data = []
        for i in range(size):
            fit_data.append({
                    'coefs': group['%s_%d_coefs'%(key, i)].value,
                    'bfOrders': group['%s_%d_bfOrders'%(key, i)].value
                    })
        return fit_data



    def get_time_deriv_from_index(self, i0, q, y):
        # Setup fit variables
        x = _utils.get_ds_fit_x(y, q)
        fit_params = _get_fit_params(x)

        # Evaluate fits
        data = self.fit_data[i0]
        ooxy_coorb = _eval_vector_fit(data['omega_orb'], 2, fit_params)
        omega = _eval_scalar_fit(data['omega'], fit_params)
        cAdot_coorb = _eval_vector_fit(data['chiA'], 3, fit_params)
        cBdot_coorb = _eval_vector_fit(data['chiB'], 3, fit_params)

        # Do rotations to the coprecessing frame, find dqdt, and append
        dydt = _utils.assemble_dydt(y, ooxy_coorb, omega,
                cAdot_coorb, cBdot_coorb)

        return dydt

    def get_time_deriv(self, t, q, y):
        """
Evaluates dydt at a given time t by interpolating dydt at 4 nearby nodes with
cubic interpolation. Use get_time_deriv_from_index when possible.
        """
        if t < self.t[0] or t > self.t[-1]:
            raise Exception("Cannot extrapolate time derivative!")
        i0 = np.argmin(abs(self.t - t))
        if t > self.t[i0]:
            imin = i0-1
        else:
            imin = i0-2
        imin = min(max(0, imin), len(self.t)-4)
        dydts = np.array([self.get_time_deriv_from_index(imin+i, q, y)
            for i in range(4)])

        ts = self.t[imin:imin+4]
        dydt = np.array([spline(ts, x)(t) for x in dydts.T])
        return dydt

    def get_omega(self, i0, q, y):
        x = _utils.get_ds_fit_x(y, q)
        fit_params = _get_fit_params(x)
        omega = _eval_scalar_fit(self.fit_data[i0]['omega'], fit_params)
        return omega

    def _get_t_ref(self, omega_ref, q, chiA0, chiB0, init_orbphase, init_quat):
        if omega_ref > 0.201:
            raise Exception("Got omega_ref = %0.4f > 0.2, too "
                    "large!"%(omega_ref))

        y0 = np.append(np.array([1., 0., 0., 0., init_orbphase]),
                np.append(chiA0, chiB0))

        if init_quat is not None:
            y0[:4] = init_quat

        omega0 = self.get_omega(0, q, y0)
        if omega_ref < omega0:
            raise Exception("Got omega_ref = %0.4f < %0.4f = omega_0, "
                    "too small!"%(omega_ref, omega0))

        # i0=0 is a lower bound, find the first index where omega > omega_ref
        imax=1
        omega_max = self.get_omega(imax, q, y0)
        omega_min = omega0
        while omega_max <= omega_ref:
            imax += 1
            omega_min = omega_max
            omega_max = self.get_omega(imax, q, y0)

        # Interpolate
        t_ref = (self.t[imax-1] * (omega_max - omega_ref)
            + self.t[imax] * (omega_ref - omega_min))/(omega_max - omega_min)

        if t_ref < self.t[0] or t_ref > self.t[-1]:
            raise Exception("Somehow, t_ref ended up being outside of "
                    "the time domain limits!")

        return t_ref

    def __call__(self, q, chiA0, chiB0, init_quat=None, init_orbphase=0.0,
                 t_ref=None, omega_ref=None):
        """
Computes the modeled NR dynamics given the initial conditions.

Arguments:
=================
q: The mass ratio
chiA0: The chiA vector at the reference time, given in the coprecessing frame
chiB0: The chiB vector at the reference time, given in the coprecessing frame
init_quat: The quaternion giving the rotation to the coprecessing frame at the
           reference time. By default, this will be the identity quaternion,
           indicating the coprecessing frame is aligned with the inertial frame.
init_orbphase: The orbital phase in the coprecessing frame at the reference time
t_ref: The reference (dimensionless) time, where the peak amplitude occurs at t=0.
       Default: The initial time, t_0/M = -4500.
omega_ref: The dimensionless orbital angular frequency used to determine t_ref,
       which is the time derivative of the orbital phase in the coprecessing frame.
       Specify at most one of t_ref, omega_ref.

Returns:
==================
q_copr: The quaternion representing the coprecessing frame with shape (4, L)
orbphase: The orbital phase in the coprecessing frame with shape (L, )
chiA: The time-dependent chiA in the coprecessing frame with shape (L, 3)
chiB: The time-dependent chiB in the coprecessing frame with shape (L, 3)

L = len(self.t), and these returned arrays are sampled at self.t
        """

        if t_ref is not None and omega_ref is not None:
            raise Exception("Specify at most one of t_ref, omega_ref.")


        normA = np.sqrt(np.sum(chiA0**2))
        normB = np.sqrt(np.sum(chiB0**2))
        maxNorm = max(normA, normB)
        if maxNorm > 1.001:
            raise Exception("Got a spin magnitude of %s > 1.0"%(maxNorm))

        if omega_ref is not None:
            t_ref = self._get_t_ref(omega_ref, q, chiA0, chiB0, init_orbphase,
                init_quat)

        y_of_t, i0 = self._initialize(q, chiA0, chiB0, init_quat,
                init_orbphase, t_ref, normA, normB)

        if i0 == 0:
            # Just gonna send it!
            k_ab4, dt_ab4, y_of_t = self._initial_RK4(q, y_of_t, normA, normB)
            y_of_t = self._integrate_forward(q, y_of_t, normA, normB, 3,
                    k_ab4, dt_ab4)

        elif i0 > 2:
            # Initialize by taking 3 steps backwards with RK4
            k_ab4 = [None, None, None]
            for i in range(3):
                y_of_t, tmp_k = self._one_backward_RK4_step(q, y_of_t,
                    normA, normB, i0-i)
                k_ab4[i] = tmp_k

            dt_array = np.append(2 * self.diff_t[:6:2], self.diff_t[6:])
            dt_ab4 = dt_array[i0-3:i0][::-1]
            self._integrate_backward(q, y_of_t, normA, normB, i0-3, k_ab4,
                dt_ab4)
            tmp_k = self.get_time_deriv_from_index(i0, q, y_of_t[i0-3])
            k_ab4 = [tmp_k, k_ab4[2], k_ab4[1]]
            dt_ab4 = dt_ab4[::-1]
            self._integrate_forward(q, y_of_t, normA, normB, i0, k_ab4, dt_ab4)
        else:
            # Initialize by taking 3 steps forwards with RK4
            k_ab4 = [None, None, None]
            for i in range(3):
                y_of_t, tmp_k = self._one_forward_RK4_step(q, y_of_t,
                    normA, normB, i0+i)
                k_ab4[i] = tmp_k

            dt_array = np.append(2 * self.diff_t[:6:2], self.diff_t[6:])
            dt_ab4 = dt_array[i0:i0+3]
            self._integrate_forward(q, y_of_t, normA, normB, i0+3, k_ab4,
                dt_ab4)
            tmp_k = self.get_time_deriv_from_index(i0+3, q, y_of_t[i0+3])
            k_ab4 = [tmp_k, k_ab4[2], k_ab4[1]]
            dt_ab4 = dt_ab4[::-1]
            self._integrate_backward(q, y_of_t, normA, normB, i0, k_ab4,
                dt_ab4)

        quat = y_of_t[:, :4].T
        orbphase = y_of_t[:, 4]
        chiA_copr = y_of_t[:, 5:8]
        chiB_copr = y_of_t[:, 8:]

        return quat, orbphase, chiA_copr, chiB_copr

    def _initialize(self, q, chiA0, chiB0, init_quat, init_orbphase, t_ref,
            normA, normB):
        """
Initializes an array of data with the initial conditions.
If t_ref does not correspond to a time node, takes one small time step to
the nearest time node.
        """
        # data is [q0, qx, qy, qz, orbphase, chiAx, chiAy, chiAz, chiBx,
        #   chiBy, chiBz]
        # We do three steps of RK4, so we have 3 fewer timesteps in the output
        # compared to self.t
        data = np.zeros((self.L-3, 11))

        y0 = np.append(np.array([1., 0., 0., 0., init_orbphase]),
                np.append(chiA0, chiB0))

        if init_quat is not None:
            y0[:4] = init_quat

        if t_ref is None:
            data[0, :] = y0
            i0 = 0
        else:
            # Step to the closest time node using forward Euler
            times = np.append(self.t[:6:2], self.t[6:])
            i0 = np.argmin(abs(times - t_ref))
            t0 = times[i0]
            dydt0 = self.get_time_deriv(t_ref, q, y0)
            y_node = y0 + (t0 - t_ref) * dydt0
            y_node = _utils.normalize_y(y_node, normA, normB)
            data[i0, :] = y_node

        return data, i0

    def _initial_RK4(self, q, y_of_t, normA, normB):
        """This is used to initialize the AB4 system when t_ref=t_0"""

        # Three steps of RK4
        k_ab4 = []
        dt_ab4 = []
        for i, dt in enumerate(self.diff_t[:6:2]):
            k1 = self.get_time_deriv_from_index(2*i, q, y_of_t[i])
            k_ab4.append(k1)
            dt_ab4.append(2*dt)
            k2 = self.get_time_deriv_from_index(2*i+1, q, y_of_t[i] + dt*k1)
            k3 = self.get_time_deriv_from_index(2*i+1, q, y_of_t[i] + dt*k2)
            k4 = self.get_time_deriv_from_index(2*i+2, q, y_of_t[i] + 2*dt*k3)
            ynext = y_of_t[i] + (dt/3.)*(k1 + 2*k2 + 2*k3 + k4)
            y_of_t[i+1] = _utils.normalize_y(ynext, normA, normB)

        return k_ab4, dt_ab4, y_of_t

    def _one_forward_RK4_step(self, q, y_of_t, normA, normB, i0):
        """Steps forward one step using RK4"""

        # i0 is on the y_of_t grid, which has 3 fewer samples than the
        # self.t grid
        i_t = i0 + 3
        if i0 < 3:
            i_t = i0*2

        t1 = self.t[i_t]
        t2 = self.t[i_t + 1]
        if i0 < 3:
            t2 = self.t[i_t + 2]
        half_dt = 0.5*(t2 - t1)

        k1 = self.get_time_deriv(t1, q, y_of_t[i0])
        k2 = self.get_time_deriv(t1 + half_dt, q, y_of_t[i0] + half_dt*k1)
        k3 = self.get_time_deriv(t1 + half_dt, q, y_of_t[i0] + half_dt*k2)
        k4 = self.get_time_deriv(t2, q, y_of_t[i0] + 2*half_dt*k3)
        ynext = y_of_t[i0] + (half_dt/3.)*(k1 + 2*k2 + 2*k3 + k4)
        y_of_t[i0+1] = _utils.normalize_y(ynext, normA, normB)
        return y_of_t, k1

    def _one_backward_RK4_step(self, q, y_of_t, normA, normB, i0):
        """Steps backward one step using RK4"""

        # i0 is on the y_of_t grid, which has 3 fewer samples than the
        # self.t grid
        i_t = i0 + 3
        if i0 < 3:
            i_t = i0*2

        t1 = self.t[i_t]
        t2 = self.t[i_t - 1]
        if i0 <= 3:
            t2 = self.t[i_t - 2]
        half_dt = 0.5*(t2 - t1)
        quarter_dt = 0.5*half_dt

        k1 = self.get_time_deriv(t1, q, y_of_t[i0])
        k2 = self.get_time_deriv(t1 + half_dt, q, y_of_t[i0] + half_dt*k1)
        k3 = self.get_time_deriv(t1 + half_dt, q, y_of_t[i0] + half_dt*k2)
        k4 = self.get_time_deriv(t2, q, y_of_t[i0] + 2*half_dt*k3)
        ynext = y_of_t[i0] + (half_dt/3.)*(k1 + 2*k2 + 2*k3 + k4)
        y_of_t[i0-1] = _utils.normalize_y(ynext, normA, normB)
        return y_of_t, k1

    def _integrate_forward(self, q, y_of_t, normA, normB, i0, k_ab4, dt_ab4):
        """
Use AB4 to integrate forward in time, starting at index i0.
i0 refers to the index of y_of_t, which should be the latest index at which
we already have the solution; typically i0=3 after three steps of RK4.
k_ab4 is [dydt(i0 - 3), dydt(i0 - 2), dydt(i0 - 1)]
dt_ab4 is [t(i0 - 2) - t(i0 - 3), t(i0 - 1) - t(i0 - 2), t(i0) - t(i0 - 1)]
where for both k_ab4 and dt_ab4 the indices correspond to y_of_t nodes and skip
fractional nodes.
        """
        if i0 < 3:
            raise Exception("i0 must be at least 3!")

        # Setup AB4
        k1, k2, k3 = k_ab4
        dt1, dt2, dt3 = dt_ab4

        # Run AB4   (i0+3 due to 3 half time steps)
        for i, dt4 in enumerate(self.diff_t[i0+3:]):
            i_output = i0+i
            k4 = self.get_time_deriv_from_index(i_output+3, q,
                    y_of_t[i_output])

            ynext = y_of_t[i_output] + _utils.ab4_dy(k1, k2, k3, k4, dt1,
                    dt2, dt3, dt4)

            y_of_t[i_output+1] = _utils.normalize_y(ynext, normA, normB)

            # Setup for next iteration
            k1, k2, k3 = k2, k3, k4
            dt1, dt2, dt3 = dt2, dt3, dt4

        return y_of_t

    def _integrate_backward(self, q, y_of_t, normA, normB, i0, k_ab4, dt_ab4):
        """
Use AB4 to integrate backward in time, starting at index i0.
k_ab4 is [dydt(i0 + 3), dydt(i0 + 2), dydt(i0 + 1)]
dt_ab4 is [t(i0 + 3) - t(i0 + 2), t(i0 + 2) - t(i0 + 1), t(i0 + 1) - t(i0)]
        """

        if i0 > len(self.t) - 7:
            raise Exception("i0 must be <= len(self.t) - 7")

        # Setup AB4
        k1, k2, k3 = k_ab4
        dt1, dt2, dt3 = dt_ab4

        # Setup dt array, removing the half steps
        dt_array = np.append(2 * self.diff_t[:6:2], self.diff_t[6:])
        for i_output in range(i0)[::-1]:
            node_index = i_output + 4
            if i_output < 2:
                node_index = 2 + 2*i_output
            dt4 = dt_array[i_output]
            k4 = self.get_time_deriv_from_index(node_index, q,
                    y_of_t[i_output+1])

            ynext = y_of_t[i_output+1] - _utils.ab4_dy(k1, k2, k3, k4,
                    dt1, dt2, dt3, dt4)

            y_of_t[i_output] = _utils.normalize_y(ynext, normA, normB)

            # Setup for next iteration
            k1, k2, k3 = k2, k3, k4
            dt1, dt2, dt3 = dt2, dt3, dt4

        return y_of_t
#########################################################

# Utility functions for the CoorbitalWaveformSurrogate:

def _extract_component_data(h5_group):
    data = {}
    data['EI_basis'] = h5_group['EIBasis'].value
    data['nodeIndices'] = h5_group['nodeIndices'].value
    data['coefs'] = [h5_group['nodeModelers']['coefs_%s'%(i)].value
                     for i in range(len(data['nodeIndices']))]
    data['orders'] = [h5_group['nodeModelers']['bfOrders_%s'%(i)].value
                      for i in range(len(data['nodeIndices']))]
    return data

def _eval_comp(data, q, chiA, chiB):
    nodes = []
    for orders, coefs, ni in zip(data['orders'], data['coefs'],
            data['nodeIndices']):

        fit_data = {
            'bfOrders': orders,
            'coefs': coefs,
            }
        x = np.append(q, np.append(chiA[ni], chiB[ni]))
        fit_params = _get_fit_params(x)
        nodes.append(_eval_scalar_fit(fit_data, fit_params))

    return np.array(nodes).dot(data['EI_basis'])

def _assemble_mode_pair(rep, rem, imp, imm):
    hplus = rep + 1.j*imp
    hminus = rem + 1.j*imm
    # hplus and hminus were built with the (ell, -m) mode as the
    # reference mode:
    #   hplus = 0.5*( h^{ell, -m} + h^{ell, m}* )
    #   hminus = 0.5*(h^{ell, -m} - h^{ell, m}* )
    return (hplus - hminus).conjugate(), hplus + hminus

#########################################################

class CoorbitalWaveformSurrogate:
    """This surrogate models the waveform in the coorbital frame."""

    def __init__(self, h5file):
        self.ellMax = 2
        while 'hCoorb_%s_%s_Re+'%(self.ellMax+1, self.ellMax+1) in h5file.keys():
            self.ellMax += 1

        self.t = h5file['t_coorb'].value

        self.data = {}
        for ell in range(2, self.ellMax+1):
            # m=0 is different
            for reim in ['real', 'imag']:
                group = h5file['hCoorb_%s_0_%s'%(ell, reim)]
                self.data['%s_0_%s'%(ell, reim)] \
                        = _extract_component_data(group)

            for m in range(1, ell+1):
                for reim in ['Re', 'Im']:
                    for pm in ['+', '-']:
                        group = h5file['hCoorb_%s_%s_%s%s'%(ell, m, reim, pm)]
                        tmp_data = _extract_component_data(group)
                        self.data['%s_%s_%s%s'%(ell, m, reim, pm)] = tmp_data


    def __call__(self, q, chiA, chiB, ellMax=4):
        """
Evaluates the coorbital waveform modes.
q: The mass ratio
chiA, chiB: The time-dependent spin in the coorbital frame. These should have
            shape (N, 3) where N = len(t_coorb)
ellMax: The maximum ell mode to evaluate.
        """
        nmodes = ellMax*ellMax + 2*ellMax - 3
        modes = 1.j*np.zeros((nmodes, len(self.t)))

        for ell in range(2, ellMax+1):
            # m=0 is different
            re = _eval_comp(self.data['%s_0_real'%(ell)], q, chiA, chiB)
            im = _eval_comp(self.data['%s_0_imag'%(ell)], q, chiA, chiB)
            modes[ell*(ell+1) - 4] = re + 1.j*im

            for m in range(1, ell+1):
                rep = _eval_comp(self.data['%s_%s_Re+'%(ell, m)], q, chiA,chiB)
                rem = _eval_comp(self.data['%s_%s_Re-'%(ell, m)], q, chiA,chiB)
                imp = _eval_comp(self.data['%s_%s_Im+'%(ell, m)], q, chiA,chiB)
                imm = _eval_comp(self.data['%s_%s_Im-'%(ell, m)], q, chiA,chiB)
                h_posm, h_negm = _assemble_mode_pair(rep, rem, imp, imm)
                modes[ell*(ell+1) - 4 + m] = h_posm
                modes[ell*(ell+1) - 4 - m] = h_negm

        return modes

##############################################################################

# Utility functions for the NRSurrogate7dq2 class:

def rotate_spin(chi, phase):
    """For transforming spins between the coprecessing and coorbital frames"""
    v = chi.T
    sp = np.sin(phase)
    cp = np.cos(phase)
    res = 1.*v
    res[0] = v[0]*cp + v[1]*sp
    res[1] = v[1]*cp - v[0]*sp
    return res.T

def coorb_spins_from_copr_spins(chiA_copr, chiB_copr, orbphase):
    chiA_coorb = rotate_spin(chiA_copr, orbphase)
    chiB_coorb = rotate_spin(chiB_copr, orbphase)
    return chiA_coorb, chiB_coorb

def inertial_waveform_modes(t, orbphase, quat, h_coorb):
    q_rot = np.array([np.cos(orbphase / 2.), 0. * orbphase,
                      0. * orbphase, np.sin(orbphase / 2.)])
    qfull = multiplyQuats(quat, q_rot)
    h_inertial = rotateWaveform(qfull, h_coorb)
    return h_inertial

def splinterp_many(t_in, t_out, many_things):
    return np.array([spline(t_in, thing)(t_out) for thing in many_things])

def mode_sum(h_modes, ellMax, theta, phi):
    coefs = []
    for ell in range(2, ellMax+1):
        for m in range(-ell, ell+1):
            coefs.append(sYlm(-2, ell, m, theta, phi))
    return np.array(coefs).dot(h_modes)

def normalize_spin(chi, chi_norm):
    if chi_norm > 0.:
        tmp_norm = np.sqrt(np.sum(chi**2, 1))
        return (chi.T * chi_norm / tmp_norm).T
    return chi

##############################################################################

class PrecessingSurrogate(object):
    """
A wrapper class for the precessing surrogate models.

See the __call__ method on how to evaluate waveforms.
    """

    def __init__(self, filename):
        """
Loads the surrogate model data.

filename: The hdf5 file containing the surrogate data."
        """
        h5file = h5py.File(filename, 'r')
        self.dynamics_sur = DynamicsSurrogate(h5file)
        self.coorb_sur = CoorbitalWaveformSurrogate(h5file)
        self.t_coorb = self.coorb_sur.t
        self.tds = np.append(self.dynamics_sur.t[0:6:2], \
            self.dynamics_sur.t[6:])

        self.t_0 = self.t_coorb[0]
        self.t_f = self.t_coorb[-1]

    def get_dynamics(self, q, chiA0, chiB0, init_phase=0.0, init_quat=None,
                     t_ref=None, omega_ref=None):
        """
Evaluates only the dynamics surrogate.
q: The mass ratio mA/mB, with 1 <= q <= 2.
chiA0, chiB0: The dimensionless black hole spins, given in the
              coprecessing frame at the reference time
init_phase: The orbital phase $\\varphi(t_ref)$ at the reference time
init_quat: The unit quaternion representing the coprecessing frame at the
           reference time.
           If None, the coprecessing frame and inertial frames will be
           aligned, and the spins can be given in the inertial frame.
t_ref: The reference (dimensionless) time, where the peak amplitude occurs at t=0.
       Default: The initial time, t_0/M = -4500.
omega_ref: The orbital angular frequency in the coprecessing frame, used to
           determine t_ref.
       Specify at most one of t_ref, fM_ref.

Returns:
q_copr: The quaternion representing the coprecessing frame with shape (4, L)
orbphase: The orbital phase in the coprecessing frame with shape (L, )
chiA: The time-dependent chiA with shape (L, 3)
chiB: The time-dependent chiB with shape (L, 3)

These are sampled on self.tds which has length L.
        """
        return self.dynamics_sur(q, chiA0, chiB0, init_orbphase=init_phase,
                 init_quat=init_quat, t_ref=t_ref, omega_ref=omega_ref)

    def get_coorb_waveform(self, q, chiA_coorb, chiB_coorb, ellMax=4):
        """
Evaluates the coorbital waveform surrogate.
q: The mass ratio mA/mB, with 1 <= q <=2.
chiA_coorb, chiB_coorb: The spins in the coorbital frame, with shape (N, 3)
    where N = len(self.t_coorb).
ellMax: The maximum ell mode to evaluate.

Returns a 2d array with shape (n_modes, N) where the modes are ordered:
    (2, -2), ..., (2, 2), (3, -3), ...
with n_modes = 5, 12, or 21 for ellMax = 2, 3, or 4 respectively
        """
        return self.coorb_sur(q, chiA_coorb, chiB_coorb, ellMax=ellMax)

    def get_time_from_freq(self, freq, q, chiA0, chiB0, init_phase=0.0,
        init_quat=None, t_ref=None, fM_ref=None):
        """
Obtain the time at which a particular gravitational wave frequency occurs.
freq: The gravitational wave frequency.
See the __call__ docstring for other parameters.
        """
        # Determine omega_ref if needed
        omega_ref = None
        if fM_ref is not None:
            omega_ref = fM_ref * np.pi

        # Get freqs vs time
        quat, orbphase, chiA_copr, chiB_copr = self.get_dynamics(
                q, chiA0, chiB0, init_phase=init_phase, init_quat=init_quat,
                t_ref=t_ref, omega_ref=omega_ref)

        omega = np.gradient(orbphase, self.tds)
        freqs = omega / np.pi

        # Find the first time where freqs >= freq, and interpolate to find the time
        if freqs[0] > freq:
            raise Exception("Frequency %s too low: initial frequency is %s"%(
                    freq, freq[0]))
        if np.max(freqs) < freq:
            raise Exception("Frequency %s too high: maximum frequency is %s"%(
                    freq, np.max(freqs)))
        i0 = np.where(freqs >= freq)[0][0]
        t0 = np.interp(freq, freqs, self.tds)

        return t0

    def __call__(self, x, phi_ref=None, fM_low=None, fM_ref=None, dtM=None,
            timesM=None, dfM=None, freqsM=None, mode_list=None, ellMax=None,
            precessing_opts=None, tidal_opts=None, par_dict=None,
            return_dynamics=False):
        """
Evaluates a precessing surrogate model.

Arguments:
    q: The mass ratio mA/mB, with 1 <= q <=2
    chiA0, chiB0:   The initial dimensionless spins given in the coprecessing
                    frame. They should be length 3 lists or numpy arrays.
                    These are $\\vec{\chi_{1,2}^\mathrm{copr}(t_0)$ in THE PAPER.
                    Their norms should be np.sqrt(chi0**2) <= 0.8
    init_phase:     The initial orbital phase in the coprecessing frame.
                    This is $\\varphi(t_0)$ in THE PAPER.
    init_quat:      The initial unit quaternion (length 4 list or numpy array)
                    giving the rotation from the coprecessing frame to the
                    inertial frame.
                    This is $\hat{q}(t_0)$ in THE PAPER.
                    If None (default), uses the identity, in which case the spins
                    in the coprecessing frame are equal to the spins in the
                    inertial frame.
    return_spins:   flag to return the inertial frame time-dependent spins,
                    $\\vec{\chi_{1,2}(t)$.
    t:              The times at which the output should be sampled.
                    The output is interpolated from self.t_coorb using cubic
                    splines. If t=None, returns the results at self.t_coorb.
    theta, phi:     Either specify one or neither. If given, sums up the
                    waveform modes for a gravitational wave propagation
                    direction of (theta, phi) on a sphere centered on the
                    source, where theta is the polar angle and phi is the
                    azimuthal angle.
                    If not given, returns a dictionary of waveform modes h_dict
                    with (ell, m) keys such that (for example) the (2, 2) mode
                    is h_dict[2, 2].
    ellMax:           The maximum ell modes to use.
                    The NRSur7dq2 surrogate model contains modes up to L=4.
                    Using ellMax=2 or ellMax=3 reduces the evaluation time.
    t_ref:
    fM_ref:
    t_ref:          The reference (dimensionless) time, where the peak amplitude
                    occurs at t=0.
                    Default: The initial time, t_0/M = -4500.
    fM_ref:          A gravitational wave frequency used to determine t_ref,
                    taken to be $\omega / pi$ where $\omega$ is the angular
                    orbital frequency in the coprecessing frame.
                    Specify at most one of t_ref, fM_ref.
    use_lalsimulation_conventions: If True, interprets the spin directions and phi
                    using lalsimulation conventions. Specifically, before evaluating
                    the surrogate, the spins will be rotated about the z-axis by
                    init_phase, and pi/2 will be added to phi if it is given.
                    This agrees with lalsimulation's ChooseTDWaveform but not
                    ChooseTDModes; set this to false to agree with ChooseTDModes.
                    This is as of June 2018.

Returns:
    h (with return_spins=False)
  or
    h, chiA, chiB (with return_spins=True)

    h: If theta and phi are specified, h is a complex 1d array sampled at times
       t (or self.t_coorb if t=None).
       Otherwise, h is a dictionary with length-2 integer tuples (ell, m) keys,
       and complex 1d arrays giving the (ell, m) mode as values.
    chiA, chiB: The inertial frame spins with shape (N, 3), where N=len(t)
                (or len(self.t_coorb) if t=None).

        """

        if dfM is not None:
            raise ValueError('Expected dfM to be None for a Time domain model')
        if freqsM is not None:
            raise ValueError('Expected freqsM to be None for a Time domain'
                ' model')

        if par_dict is not None:
            raise ValueError('par_dict should be None for this model')

        if precessing_opts is None:
            precessing_opts = {}


        init_phase = precessing_opts.pop('init_phase', 0)
        init_quat = precessing_opts.pop('init_quat', None)
        return_dynamics = precessing_opts.pop('return_dynamics', False)
        use_lalsimulation_conventions \
            = precessing_opts.pop('use_lalsimulation_conventions', False)

        if ellMax is None:
            ellMax = 4

        q, chiA0, chiB0 = x

        if use_lalsimulation_conventions:
            # rotate_spin rotates in the -z direction
            chiA0 = rotate_spin(chiA0, -1 * init_phase)
            chiB0 = rotate_spin(chiB0, -1 * init_phase)
            if phi is not None:
                phi += 0.5 * np.pi

        chiA_norm = np.sqrt(np.sum(chiA0**2))
        chiB_norm = np.sqrt(np.sum(chiB0**2))

        ## Get dynamics

        # Get dimensionless omega_ref
        if fM_ref is None or fM_ref == 0:
            omega_ref = None
        else:
            omega_ref = fM_ref * np.pi

        quat, orbphase, chiA_copr, chiB_copr = self.get_dynamics(
                q, chiA0, chiB0, init_phase=init_phase, init_quat=init_quat,
                t_ref=None, omega_ref=omega_ref)

        # Interpolate to the coorbital time grid, and transform to coorb frame.
        # Interpolate first since coorbital spins oscillate faster than
        # coprecessing spins
        chiA_copr = splinterp_many(self.tds, self.t_coorb, chiA_copr.T).T
        chiB_copr = splinterp_many(self.tds, self.t_coorb, chiB_copr.T).T
        chiA_copr = normalize_spin(chiA_copr, chiA_norm)
        chiB_copr = normalize_spin(chiB_copr, chiB_norm)
        orbphase = spline(self.tds, orbphase)(self.t_coorb)
        quat = splinterp_many(self.tds, self.t_coorb, quat)
        quat = quat/np.sqrt(np.sum(abs(quat)**2, 0))
        chiA_coorb, chiB_coorb = coorb_spins_from_copr_spins(
                chiA_copr, chiB_copr, orbphase)

        # Evaluate coorbital waveform surrogate
        h_coorb = self.get_coorb_waveform(q, chiA_coorb, chiB_coorb, ellMax=ellMax)

        # Transform the sparsely sampled waveform
        h_inertial = inertial_waveform_modes(self.t_coorb, orbphase, quat,
                h_coorb)

        if timesM is not None:
            if timesM[-1] > self.t_coorb[-1]:
                raise Exception("'times' includes times larger than the"
                    " maximum time value in domain.")
            if timesM[0] < self.t_coorb[0]:
                raise Exception("'times' starts before start of domain. Try"
                    " increasing initial value of times or reducing f_low.")

        return_times = True
        if dtM is None and timesM is None:
            # Use the sparse domain
            timesM = self.t_coorb
            do_interp = False
        else:
            ## Interpolate onto uniform domain if needed
            do_interp = True
            if dtM is not None:
                # FIXME use fM_low
                t0 = self.t_coorb[0]
                tf = self.t_coorb[-1]
                num_times = int(np.ceil((tf - t0)/dtM));
                timesM = t0 + dtM*np.arange(num_times)
            else:
                return_times = False


        if do_interp:
            hre = splinterp_many(self.t_coorb, timesM, np.real(h_inertial))
            him = splinterp_many(self.t_coorb, timesM, np.imag(h_inertial))
            h_inertial = hre + 1.j*him

        # Make mode dict
        h = {}
        i=0
        for ell in range(2, ellMax+1):
            for m in range(-ell, ell+1):
                h[(ell, m)] = h_inertial[i]
                i += 1

        #  Transform and interpolate spins if needed
        if return_dynamics:
            chiA_inertial = transformTimeDependentVector(quat, chiA_copr.T).T
            chiB_inertial = transformTimeDependentVector(quat, chiB_copr.T).T
            if do_interp:
                chiA_inertial = splinterp_many(self.t_coorb, timesM,
                        chiA_inertial.T).T

                chiB_inertial = splinterp_many(self.t_coorb, timesM,
                        chiB_inertial.T).T

                chiA_inertial = normalize_spin(chiA_inertial, chiA_norm)
                chiB_inertial = normalize_spin(chiB_inertial, chiB_norm)

                orbphase = spline(self.t_coorb, orbphase)(timesM)
                quat = splinterp_many(self.t_coorb, timesM, quat)
                quat = quat/np.sqrt(np.sum(abs(quat)**2, 0))

                dynamics = {
                    'chiA': chiA_inertial,
                    'chiB': chiB_inertial,
                    'q_copr': quat,
                    'orbphase': orbphase,
                    }
        else:
            dynamics = None

        return timesM, h, dynamics