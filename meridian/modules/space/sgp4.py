"""Orbit maths for the space module: SGP4 propagation, frames, the Sun and pass prediction.

Written from the published algorithm (Hoots and Roehrich, Spacetrack Report #3, 1980, as revised by
Vallado, Crawford, Hujsak and Kelso, "Revisiting Spacetrack Report #3", AIAA 2006-6753), with the
WGS-72 constants that general-perturbations element sets are fitted with. Pure Python, no imports
beyond ``math``.

Accuracy (see docs/modules/space.md and tests/test_space.py):

* Near-Earth objects (period under 225 minutes, which covers the ISS, Earth observation and
  weather satellites, Starlink): full SGP4. It agrees with the browser's satellite.js to well
  under a metre; the elements themselves are good to roughly 1 km at epoch, growing a few km a day.
* Deep-space objects (period of 225 minutes or more: navigation, geostationary, Molniya): the
  lunar-solar and resonance terms of SDP4 are not implemented. They are propagated with the same
  secular J2/J4 and short-period terms only, which stays within a few tens of km of SDP4 over a
  day or two. That is far below what matters for "is it above my horizon".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

TWO_PI = 2.0 * math.pi
DEG = math.pi / 180.0
RAD = 180.0 / math.pi

# WGS-72 (the constants SGP4 elements are fitted with)
MU = 398600.8                      # km^3 / s^2
RE = 6378.135                      # km
XKE = 60.0 / math.sqrt(RE * RE * RE / MU)
J2 = 0.001082616
J3 = -0.00000253881
J4 = -0.00000165597
J3OJ2 = J3 / J2
X2O3 = 2.0 / 3.0
VKMPERSEC = RE * XKE / 60.0
MIN_PER_DAY = 1440.0

# WGS-84 ellipsoid for geodetic conversions
WGS84_A = 6378.137
WGS84_B = 6356.7523142
WGS84_F = (WGS84_A - WGS84_B) / WGS84_A
WGS84_E2 = 2.0 * WGS84_F - WGS84_F * WGS84_F

EARTH_ROT = 7.292115e-5            # rad / s
AU_KM = 149597870.7
JD_UNIX_EPOCH = 2440587.5


class PropagationError(Exception):
    """SGP4 could not produce a position (decayed, or elements out of range)."""


def jd_from_unix(t: float) -> float:
    return t / 86400.0 + JD_UNIX_EPOCH


def unix_from_jd(jd: float) -> float:
    return (jd - JD_UNIX_EPOCH) * 86400.0


def parse_epoch(text: str) -> float:
    """OMM ``EPOCH`` ("2026-11-22T13:24:24.463872", UTC, Z optional) -> epoch seconds."""
    s = str(text).strip()
    if s.endswith("Z"):
        s = s[:-1]
    frac = 0.0
    if "." in s:
        s, _, digits = s.partition(".")
        digits = "".join(ch for ch in digits if ch.isdigit())
        frac = float("0." + digits) if digits else 0.0
    dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.timestamp() + frac


def gstime(jdut1: float) -> float:
    """Greenwich mean sidereal time (radians, 0..2pi) for a UT1 Julian date (IAU 1982)."""
    tut1 = (jdut1 - 2451545.0) / 36525.0
    temp = (-6.2e-6 * tut1 * tut1 * tut1 + 0.093104 * tut1 * tut1
            + (876600.0 * 3600.0 + 8640184.812866) * tut1 + 67310.54841)
    temp = math.fmod(temp * DEG / 240.0, TWO_PI)
    if temp < 0.0:
        temp += TWO_PI
    return temp


# ---------------------------------------------------------------------------------------------
# SGP4
# ---------------------------------------------------------------------------------------------
class Satrec:
    """Initialised SGP4 state for one element set (build with :func:`satrec_from_omm`)."""

    __slots__ = ("satnum", "jdsatepoch", "epoch", "bstar", "ecco", "argpo", "inclo", "mo", "no_kozai",
                 "nodeo", "no_unkozai", "a", "alta", "altp", "isimp", "deep", "con41", "x1mth2", "x7thm1",
                 "cc1", "cc4", "cc5", "d2", "d3", "d4", "delmo", "eta", "argpdot", "omgcof", "sinmao",
                 "t2cof", "t3cof", "t4cof", "t5cof", "mdot", "nodedot", "nodecf", "xlcof", "aycof", "xmcof",
                 "gsto", "error")

    def __init__(self) -> None:
        for name in self.__slots__:
            setattr(self, name, 0.0)
        self.isimp = 0
        self.deep = False
        self.error = 0

    @property
    def period_min(self) -> float:
        return TWO_PI / self.no_unkozai


def sgp4init(satnum: int, epoch_unix: float, bstar: float, ecco: float, argpo: float, inclo: float,
             mo: float, no_kozai: float, nodeo: float) -> Satrec:
    """Initialise SGP4 ('improved' operation mode). Angles in radians, ``no_kozai`` in rad/min."""
    s = Satrec()
    s.satnum = satnum
    s.epoch = epoch_unix
    s.jdsatepoch = jd_from_unix(epoch_unix)
    s.bstar = bstar
    s.ecco = ecco
    s.argpo = argpo
    s.inclo = inclo
    s.mo = mo
    s.no_kozai = no_kozai
    s.nodeo = nodeo
    if not (no_kozai > 0.0) or not (0.0 <= ecco < 1.0):
        raise PropagationError("element set out of range")

    ss = 78.0 / RE + 1.0
    qzms2ttemp = (120.0 - 78.0) / RE
    qzms2t = qzms2ttemp * qzms2ttemp * qzms2ttemp * qzms2ttemp
    temp4 = 1.5e-12

    # -- initl: recover the original mean motion and semi-major axis -------------------------
    eccsq = ecco * ecco
    omeosq = 1.0 - eccsq
    rteosq = math.sqrt(omeosq)
    cosio = math.cos(inclo)
    cosio2 = cosio * cosio
    ak = (XKE / no_kozai) ** X2O3
    d1 = 0.75 * J2 * (3.0 * cosio2 - 1.0) / (rteosq * omeosq)
    del_ = d1 / (ak * ak)
    adel = ak * (1.0 - del_ * del_ - del_ * (1.0 / 3.0 + 134.0 * del_ * del_ / 81.0))
    del_ = d1 / (adel * adel)
    s.no_unkozai = no_kozai / (1.0 + del_)
    ao = (XKE / s.no_unkozai) ** X2O3
    sinio = math.sin(inclo)
    po = ao * omeosq
    con42 = 1.0 - 5.0 * cosio2
    s.con41 = -con42 - cosio2 - cosio2
    posq = po * po
    rp = ao * (1.0 - ecco)
    s.gsto = gstime(s.jdsatepoch)
    s.a = ao
    s.alta = ao * (1.0 + ecco) - 1.0
    s.altp = ao * (1.0 - ecco) - 1.0

    if omeosq >= 0.0 or s.no_unkozai >= 0.0:
        s.isimp = 0
        if rp < (220.0 / RE + 1.0):
            s.isimp = 1
        sfour = ss
        qzms24 = qzms2t
        perige = (rp - 1.0) * RE
        if perige < 156.0:            # for perigees below 156 km, s and qoms2t are altered
            sfour = perige - 78.0
            if perige < 98.0:
                sfour = 20.0
            qzms24 = ((120.0 - sfour) / RE) ** 4
            sfour = sfour / RE + 1.0
        pinvsq = 1.0 / posq
        tsi = 1.0 / (ao - sfour)
        s.eta = ao * ecco * tsi
        etasq = s.eta * s.eta
        eeta = ecco * s.eta
        psisq = abs(1.0 - etasq)
        coef = qzms24 * tsi ** 4
        coef1 = coef / psisq ** 3.5
        cc2 = coef1 * s.no_unkozai * (ao * (1.0 + 1.5 * etasq + eeta * (4.0 + etasq))
                                      + 0.375 * J2 * tsi / psisq * s.con41 * (8.0 + 3.0 * etasq * (8.0 + etasq)))
        s.cc1 = bstar * cc2
        cc3 = 0.0
        if ecco > 1.0e-4:
            cc3 = -2.0 * coef * tsi * J3OJ2 * s.no_unkozai * sinio / ecco
        s.x1mth2 = 1.0 - cosio2
        s.cc4 = 2.0 * s.no_unkozai * coef1 * ao * omeosq * (
            s.eta * (2.0 + 0.5 * etasq) + ecco * (0.5 + 2.0 * etasq)
            - J2 * tsi / (ao * psisq) * (-3.0 * s.con41 * (1.0 - 2.0 * eeta + etasq * (1.5 - 0.5 * eeta))
                                         + 0.75 * s.x1mth2 * (2.0 * etasq - eeta * (1.0 + etasq))
                                         * math.cos(2.0 * argpo)))
        s.cc5 = 2.0 * coef1 * ao * omeosq * (1.0 + 2.75 * (etasq + eeta) + eeta * etasq)
        cosio4 = cosio2 * cosio2
        temp1 = 1.5 * J2 * pinvsq * s.no_unkozai
        temp2 = 0.5 * temp1 * J2 * pinvsq
        temp3 = -0.46875 * J4 * pinvsq * pinvsq * s.no_unkozai
        s.mdot = (s.no_unkozai + 0.5 * temp1 * rteosq * s.con41
                  + 0.0625 * temp2 * rteosq * (13.0 - 78.0 * cosio2 + 137.0 * cosio4))
        s.argpdot = (-0.5 * temp1 * con42 + 0.0625 * temp2 * (7.0 - 114.0 * cosio2 + 395.0 * cosio4)
                     + temp3 * (3.0 - 36.0 * cosio2 + 49.0 * cosio4))
        xhdot1 = -temp1 * cosio
        s.nodedot = xhdot1 + (0.5 * temp2 * (4.0 - 19.0 * cosio2) + 2.0 * temp3 * (3.0 - 7.0 * cosio2)) * cosio
        s.omgcof = bstar * cc3 * math.cos(argpo)
        s.xmcof = 0.0
        if ecco > 1.0e-4:
            s.xmcof = -X2O3 * coef * bstar / eeta
        s.nodecf = 3.5 * omeosq * xhdot1 * s.cc1
        s.t2cof = 1.5 * s.cc1
        if abs(cosio + 1.0) > 1.5e-12:   # avoid a divide by zero at 180 degrees inclination
            s.xlcof = -0.25 * J3OJ2 * sinio * (3.0 + 5.0 * cosio) / (1.0 + cosio)
        else:
            s.xlcof = -0.25 * J3OJ2 * sinio * (3.0 + 5.0 * cosio) / temp4
        s.aycof = -0.5 * J3OJ2 * sinio
        delmotemp = 1.0 + s.eta * math.cos(mo)
        s.delmo = delmotemp * delmotemp * delmotemp
        s.sinmao = math.sin(mo)
        s.x7thm1 = 7.0 * cosio2 - 1.0

        if TWO_PI / s.no_unkozai >= 225.0:
            # Deep space. SDP4's lunar-solar and resonance terms are not modelled (module docstring).
            s.deep = True
            s.isimp = 1

        if s.isimp != 1:
            cc1sq = s.cc1 * s.cc1
            s.d2 = 4.0 * ao * tsi * cc1sq
            temp = s.d2 * tsi * s.cc1 / 3.0
            s.d3 = (17.0 * ao + sfour) * temp
            s.d4 = 0.5 * temp * ao * tsi * (221.0 * ao + 31.0 * sfour) * s.cc1
            s.t3cof = s.d2 + 2.0 * cc1sq
            s.t4cof = 0.25 * (3.0 * s.d3 + s.cc1 * (12.0 * s.d2 + 10.0 * cc1sq))
            s.t5cof = 0.2 * (3.0 * s.d4 + 12.0 * s.cc1 * s.d3 + 6.0 * s.d2 * s.d2
                             + 15.0 * cc1sq * (2.0 * s.d2 + cc1sq))
    return s


def sgp4(s: Satrec, tsince: float) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Position (km) and velocity (km/s) in the TEME frame ``tsince`` minutes after the epoch."""
    t = tsince
    xmdf = s.mo + s.mdot * t
    argpdf = s.argpo + s.argpdot * t
    nodedf = s.nodeo + s.nodedot * t
    argpm = argpdf
    mm = xmdf
    t2 = t * t
    nodem = nodedf + s.nodecf * t2
    tempa = 1.0 - s.cc1 * t
    tempe = s.bstar * s.cc4 * t
    templ = s.t2cof * t2
    if s.isimp != 1:
        delomg = s.omgcof * t
        delmtemp = 1.0 + s.eta * math.cos(xmdf)
        delm = s.xmcof * (delmtemp * delmtemp * delmtemp - s.delmo)
        temp = delomg + delm
        mm = xmdf + temp
        argpm = argpdf - temp
        t3 = t2 * t
        t4 = t3 * t
        tempa = tempa - s.d2 * t2 - s.d3 * t3 - s.d4 * t4
        tempe = tempe + s.bstar * s.cc5 * (math.sin(mm) - s.sinmao)
        templ = templ + s.t3cof * t3 + t4 * (s.t4cof + t * s.t5cof)

    nm = s.no_unkozai
    em = s.ecco
    inclm = s.inclo
    if nm <= 0.0:
        raise PropagationError("mean motion below zero")
    am = (XKE / nm) ** X2O3 * tempa * tempa
    nm = XKE / am ** 1.5
    em = em - tempe
    if em >= 1.0 or em < -0.001:
        raise PropagationError("eccentricity out of range")
    if em < 1.0e-6:
        em = 1.0e-6
    mm = mm + s.no_unkozai * templ
    xlm = mm + argpm + nodem
    nodem = math.fmod(nodem, TWO_PI)
    argpm = math.fmod(argpm, TWO_PI)
    xlm = math.fmod(xlm, TWO_PI)
    mm = math.fmod(xlm - argpm - nodem, TWO_PI)

    sinip = math.sin(inclm)
    cosip = math.cos(inclm)
    ep = em
    xincp = inclm
    argpp = argpm
    nodep = nodem
    mp = mm

    # long-period periodics
    axnl = ep * math.cos(argpp)
    temp = 1.0 / (am * (1.0 - ep * ep))
    aynl = ep * math.sin(argpp) + temp * s.aycof
    xl = mp + argpp + nodep + temp * s.xlcof * axnl

    # Kepler's equation
    u = math.fmod(xl - nodep, TWO_PI)
    eo1 = u
    tem5 = 9999.9
    ktr = 1
    sineo1 = coseo1 = 0.0
    while abs(tem5) >= 1.0e-12 and ktr <= 10:
        sineo1 = math.sin(eo1)
        coseo1 = math.cos(eo1)
        tem5 = 1.0 - coseo1 * axnl - sineo1 * aynl
        tem5 = (u - aynl * coseo1 + axnl * sineo1 - eo1) / tem5
        if abs(tem5) >= 0.95:
            tem5 = 0.95 if tem5 > 0.0 else -0.95
        eo1 = eo1 + tem5
        ktr += 1

    # short-period preliminary quantities
    ecose = axnl * coseo1 + aynl * sineo1
    esine = axnl * sineo1 - aynl * coseo1
    el2 = axnl * axnl + aynl * aynl
    pl = am * (1.0 - el2)
    if pl < 0.0:
        raise PropagationError("semi-latus rectum below zero")
    rl = am * (1.0 - ecose)
    rdotl = math.sqrt(am) * esine / rl
    rvdotl = math.sqrt(pl) / rl
    betal = math.sqrt(1.0 - el2)
    temp = esine / (1.0 + betal)
    sinu = am / rl * (sineo1 - aynl - axnl * temp)
    cosu = am / rl * (coseo1 - axnl + aynl * temp)
    su = math.atan2(sinu, cosu)
    sin2u = (cosu + cosu) * sinu
    cos2u = 1.0 - 2.0 * sinu * sinu
    temp = 1.0 / pl
    temp1 = 0.5 * J2 * temp
    temp2 = temp1 * temp

    # short-period periodics
    mrt = rl * (1.0 - 1.5 * temp2 * betal * s.con41) + 0.5 * temp1 * s.x1mth2 * cos2u
    su = su - 0.25 * temp2 * s.x7thm1 * sin2u
    xnode = nodep + 1.5 * temp2 * cosip * sin2u
    xinc = xincp + 1.5 * temp2 * cosip * sinip * cos2u
    mvt = rdotl - nm * temp1 * s.x1mth2 * sin2u / XKE
    rvdot = rvdotl + nm * temp1 * (s.x1mth2 * cos2u + 1.5 * s.con41) / XKE

    # orientation vectors
    sinsu = math.sin(su)
    cossu = math.cos(su)
    snod = math.sin(xnode)
    cnod = math.cos(xnode)
    sini = math.sin(xinc)
    cosi = math.cos(xinc)
    xmx = -snod * cosi
    xmy = cnod * cosi
    ux = xmx * sinsu + cnod * cossu
    uy = xmy * sinsu + snod * cossu
    uz = sini * sinsu
    vx = xmx * cossu - cnod * sinsu
    vy = xmy * cossu - snod * sinsu
    vz = sini * cossu

    if mrt < 1.0:
        raise PropagationError("the satellite has decayed")
    mr = mrt * RE
    return ((mr * ux, mr * uy, mr * uz),
            ((mvt * ux + rvdot * vx) * VKMPERSEC, (mvt * uy + rvdot * vy) * VKMPERSEC,
             (mvt * uz + rvdot * vz) * VKMPERSEC))


def satrec_from_omm(rec: dict) -> Satrec:
    """Build a :class:`Satrec` from a CelesTrak OMM JSON record (or the module's compact record)."""
    get = rec.get
    epoch = get("epoch_unix")
    if epoch is None:
        epoch = parse_epoch(get("EPOCH") or get("epoch"))
    mean_motion = float(get("MEAN_MOTION", get("mm")))          # revolutions per day
    return sgp4init(
        int(get("NORAD_CAT_ID", get("id"))), float(epoch),
        float(get("BSTAR", get("bstar")) or 0.0),
        float(get("ECCENTRICITY", get("ecc"))),
        float(get("ARG_OF_PERICENTER", get("argp"))) * DEG,
        float(get("INCLINATION", get("inc"))) * DEG,
        float(get("MEAN_ANOMALY", get("ma"))) * DEG,
        mean_motion * TWO_PI / MIN_PER_DAY,
        float(get("RA_OF_ASC_NODE", get("raan"))) * DEG)


def propagate(s: Satrec, t_unix: float):
    """TEME position and velocity at epoch seconds ``t_unix``."""
    return sgp4(s, (t_unix - s.epoch) / 60.0)


# ---------------------------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------------------------
def teme_to_ecef(r: tuple[float, float, float], gmst: float) -> tuple[float, float, float]:
    c = math.cos(gmst)
    s = math.sin(gmst)
    return (r[0] * c + r[1] * s, -r[0] * s + r[1] * c, r[2])


def ecef_to_geodetic(r: tuple[float, float, float]) -> tuple[float, float, float]:
    """ECEF km -> (latitude deg, longitude deg, height km) on WGS-84."""
    x, y, z = r
    p = math.sqrt(x * x + y * y)
    lon = math.atan2(y, x)
    lat = math.atan2(z, p)
    for _ in range(20):
        prev = lat
        sl = math.sin(lat)
        c = 1.0 / math.sqrt(1.0 - WGS84_E2 * sl * sl)
        lat = math.atan2(z + WGS84_A * c * WGS84_E2 * sl, p)
        if abs(lat - prev) < 1e-12:
            break
    sl = math.sin(lat)
    cl = math.cos(lat)
    c = 1.0 / math.sqrt(1.0 - WGS84_E2 * sl * sl)
    h = p * cl + z * sl - WGS84_A / c
    return lat * RAD, lon * RAD, h


def geodetic_to_ecef(lat_deg: float, lon_deg: float, h_km: float = 0.0) -> tuple[float, float, float]:
    lat = lat_deg * DEG
    lon = lon_deg * DEG
    sl = math.sin(lat)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sl * sl)
    return ((n + h_km) * math.cos(lat) * math.cos(lon),
            (n + h_km) * math.cos(lat) * math.sin(lon),
            (n * (1.0 - WGS84_E2) + h_km) * sl)


class Observer:
    """A ground point with its topocentric (south, east, zenith) basis precomputed."""

    __slots__ = ("lat", "lon", "h", "ecef", "sl", "cl", "so", "co")

    def __init__(self, lat_deg: float, lon_deg: float, h_km: float = 0.0):
        self.lat = lat_deg
        self.lon = lon_deg
        self.h = h_km
        self.ecef = geodetic_to_ecef(lat_deg, lon_deg, h_km)
        self.sl = math.sin(lat_deg * DEG)
        self.cl = math.cos(lat_deg * DEG)
        self.so = math.sin(lon_deg * DEG)
        self.co = math.cos(lon_deg * DEG)

    def look(self, r_ecef: tuple[float, float, float]) -> tuple[float, float, float]:
        """(azimuth deg from north, elevation deg, range km) of an ECEF point."""
        rx = r_ecef[0] - self.ecef[0]
        ry = r_ecef[1] - self.ecef[1]
        rz = r_ecef[2] - self.ecef[2]
        top_s = self.sl * self.co * rx + self.sl * self.so * ry - self.cl * rz
        top_e = -self.so * rx + self.co * ry
        top_z = self.cl * self.co * rx + self.cl * self.so * ry + self.sl * rz
        rng = math.sqrt(top_s * top_s + top_e * top_e + top_z * top_z)
        el = math.asin(max(-1.0, min(1.0, top_z / rng)))
        az = math.atan2(-top_e, top_s) + math.pi
        return (az * RAD) % 360.0, el * RAD, rng

    def up_dot(self, v_ecef: tuple[float, float, float]) -> float:
        """Sine of the elevation of a (unit) direction vector given in ECEF axes."""
        return self.cl * self.co * v_ecef[0] + self.cl * self.so * v_ecef[1] + self.sl * v_ecef[2]


# ---------------------------------------------------------------------------------------------
# The Sun
# ---------------------------------------------------------------------------------------------
def sun_eci(jd: float) -> tuple[float, float, float]:
    """Geocentric Sun position in AU, equator and equinox of date (low precision, ~0.01 deg)."""
    tut1 = (jd - 2451545.0) / 36525.0
    meanlong = math.fmod(280.460 + 36000.77 * tut1, 360.0)
    meananomaly = math.fmod((357.5277233 + 35999.05034 * tut1) * DEG, TWO_PI)
    if meananomaly < 0.0:
        meananomaly += TWO_PI
    eclplong = math.fmod(meanlong + 1.914666471 * math.sin(meananomaly)
                         + 0.019994643 * math.sin(2.0 * meananomaly), 360.0) * DEG
    obliquity = (23.439291 - 0.0130042 * tut1) * DEG
    magr = 1.000140612 - 0.016708617 * math.cos(meananomaly) - 0.000139589 * math.cos(2.0 * meananomaly)
    return (magr * math.cos(eclplong),
            magr * math.cos(obliquity) * math.sin(eclplong),
            magr * math.sin(obliquity) * math.sin(eclplong))


def _unit(v):
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (v[0] / n, v[1] / n, v[2] / n)


def sun_elevation(obs: Observer, t_unix: float) -> float:
    """Elevation of the Sun's centre above the observer's horizon in degrees (no refraction)."""
    jd = jd_from_unix(t_unix)
    sun = _unit(sun_eci(jd))
    s_ecef = teme_to_ecef(sun, gstime(jd))
    return math.asin(max(-1.0, min(1.0, obs.up_dot(s_ecef)))) * RAD


def subsolar_point(t_unix: float) -> tuple[float, float]:
    jd = jd_from_unix(t_unix)
    s = teme_to_ecef(_unit(sun_eci(jd)), gstime(jd))
    return math.asin(s[2]) * RAD, math.atan2(s[1], s[0]) * RAD


def in_sunlight(r_eci: tuple[float, float, float], sun_unit: tuple[float, float, float]) -> bool:
    """Cylindrical Earth-shadow test (accurate to a few seconds at the shadow edge for LEO)."""
    d = r_eci[0] * sun_unit[0] + r_eci[1] * sun_unit[1] + r_eci[2] * sun_unit[2]
    if d >= 0.0:
        return True
    px = r_eci[0] - d * sun_unit[0]
    py = r_eci[1] - d * sun_unit[1]
    pz = r_eci[2] - d * sun_unit[2]
    return px * px + py * py + pz * pz > WGS84_A * WGS84_A


# ---------------------------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------------------------
@dataclass
class State:
    t: float
    lat: float
    lon: float
    alt_km: float
    speed_km_s: float
    sunlit: bool
    r: tuple[float, float, float]
    v: tuple[float, float, float]


def state_at(s: Satrec, t_unix: float) -> State:
    r, v = propagate(s, t_unix)
    jd = jd_from_unix(t_unix)
    lat, lon, alt = ecef_to_geodetic(teme_to_ecef(r, gstime(jd)))
    sun = _unit(sun_eci(jd))
    speed = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return State(t_unix, lat, lon, alt, speed, in_sunlight(r, sun), r, v)


def footprint_radius_km(alt_km: float, min_el_deg: float = 0.0) -> float:
    """Ground radius of the area that sees the satellite above ``min_el_deg``."""
    if alt_km <= 0:
        return 0.0
    re = 6371.0
    el = min_el_deg * DEG
    lam = math.acos(re * math.cos(el) / (re + alt_km)) - el
    return max(0.0, lam * re)


def orbit_info(s: Satrec) -> dict:
    """Period, apsides and orbit class from the mean elements."""
    a_km = s.a * RE
    period = s.period_min
    perigee = s.altp * RE
    apogee = s.alta * RE
    e = s.ecco
    inc = s.inclo * RAD
    if e >= 0.25:
        cls = "HEO"
    elif apogee < 2000.0:
        cls = "LEO"
    elif abs(period - 1436.07) <= 30.0 and e < 0.05:
        cls = "GEO"
    elif period < 1406.0:
        cls = "MEO"
    else:
        cls = "HEO"
    return {"period_min": period, "semi_major_axis_km": a_km, "perigee_km": perigee, "apogee_km": apogee,
            "eccentricity": e, "inclination_deg": inc, "orbit_class": cls, "deep_space": s.deep}


COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass(az_deg: float) -> str:
    return COMPASS[int((az_deg % 360.0) / 22.5 + 0.5) % 16]


# ---------------------------------------------------------------------------------------------
# Passes
# ---------------------------------------------------------------------------------------------
class _Look:
    """Cached look-angle evaluation for one satellite and observer."""

    def __init__(self, s: Satrec, obs: Observer):
        self.s = s
        self.obs = obs

    def at(self, t: float) -> tuple[float, float, float, tuple[float, float, float]]:
        r, _v = propagate(self.s, t)
        jd = jd_from_unix(t)
        az, el, rng = self.obs.look(teme_to_ecef(r, gstime(jd)))
        return az, el, rng, r

    def el(self, t: float) -> float:
        try:
            return self.at(t)[1]
        except PropagationError:
            return -90.0


def _bisect_cross(look: _Look, t0: float, t1: float, level: float, rising: bool, tol: float = 1.0) -> float:
    """Time in [t0, t1] where elevation crosses ``level`` (rising or setting)."""
    a, b = t0, t1
    while b - a > tol:
        mid = 0.5 * (a + b)
        above = look.el(mid) >= level
        if above == rising:
            b = mid
        else:
            a = mid
    return 0.5 * (a + b)


def _peak(look: _Look, a: float, b: float, tol: float = 1.0) -> tuple[float, float]:
    """Golden-section search for the highest elevation in [a, b] -> (time, elevation)."""
    g = (math.sqrt(5.0) - 1.0) / 2.0
    c = b - g * (b - a)
    d = a + g * (b - a)
    fc = look.el(c)
    fd = look.el(d)
    while b - a > tol:
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = look.el(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = look.el(d)
    t = 0.5 * (a + b)
    return t, look.el(t)


def magnitude(std_mag: float, range_km: float, phase_angle_rad: float) -> float:
    """Visual magnitude of a diffuse sphere with standard magnitude at 1000 km and 90 deg phase."""
    phi = phase_angle_rad
    f = math.sin(phi) + (math.pi - phi) * math.cos(phi)
    if f <= 1e-6:
        return 99.0
    return std_mag + 5.0 * math.log10(range_km / 1000.0) - 2.5 * math.log10(f)


def find_passes(s: Satrec, lat: float, lon: float, start: float, hours: float = 24.0, *,
                min_el: float = 10.0, visible_only: bool = False, twilight_deg: float = -6.0,
                max_passes: int = 30, std_mag: float | None = None, alt_km: float = 0.0) -> list[dict]:
    """Passes of ``s`` over (lat, lon) above ``min_el`` between ``start`` and ``start + hours``.

    A pass is *visible* when, at some moment above ``min_el``, the satellite is in sunlight and the
    Sun is below ``twilight_deg`` for the observer (civil twilight by default). Times are epoch
    seconds; rise and set are found to about a second, the culmination to about a second.
    """
    obs = Observer(lat, lon, alt_km)
    look = _Look(s, obs)
    end = start + max(0.0, hours) * 3600.0
    leo = s.period_min < 225.0
    step = 30.0 if leo else 300.0
    passes: list[dict] = []

    t = start
    el = look.el(t)
    in_pass = el >= min_el
    rise_t = start if in_pass else None
    prev_el = el
    rising_trend = False
    guard = 0
    while t < end and len(passes) < max_passes and guard < 200000:
        guard += 1
        st = step * 4.0 if (leo and prev_el < -30.0) else step
        t2 = min(end, t + st)
        el2 = look.el(t2)
        if not in_pass:
            if el2 >= min_el:
                rise_t = _bisect_cross(look, t, t2, min_el, rising=True)
                in_pass = True
            elif rising_trend and el2 < prev_el and prev_el > min_el - 6.0:
                # a short pass may peak between two samples that are both under the threshold
                tp, ep = _peak(look, max(start, t - st), t2)
                if ep >= min_el:
                    r0 = _bisect_cross(look, max(start, t - st), tp, min_el, rising=True)
                    r1 = _bisect_cross(look, tp, t2, min_el, rising=False)
                    p = _describe(look, r0, r1, start, end, min_el, twilight_deg, std_mag, False, False)
                    if p and (p["visible"] or not visible_only):
                        passes.append(p)
        else:
            if el2 < min_el:
                set_t = _bisect_cross(look, t, t2, min_el, rising=False)
                p = _describe(look, rise_t, set_t, start, end, min_el, twilight_deg, std_mag,
                              rise_t <= start, False)
                if p and (p["visible"] or not visible_only):
                    passes.append(p)
                in_pass = False
                rise_t = None
        rising_trend = el2 > prev_el
        prev_el = el2
        t = t2
    if in_pass and rise_t is not None and len(passes) < max_passes:
        p = _describe(look, rise_t, end, start, end, min_el, twilight_deg, std_mag, rise_t <= start, True)
        if p and (p["visible"] or not visible_only):
            passes.append(p)
    return passes


def _describe(look: _Look, rise: float, set_: float, start: float, end: float, min_el: float,
              twilight_deg: float, std_mag: float | None, started: bool, unfinished: bool) -> dict | None:
    if set_ <= rise:
        return None
    t_max, el_max = _peak(look, rise, set_)
    try:
        az_r = look.at(rise)[0]
        az_m, _e, rng_m, _r = look.at(t_max)
        az_s = look.at(set_)[0]
    except PropagationError:
        return None
    obs = look.obs
    # walk the pass in 10 s steps for sunlight, darkness and brightness
    n = max(2, int((set_ - rise) / 10.0) + 1)
    vis_start = vis_end = None
    vis_max_el = None
    best_mag = None
    sunlit_any = False
    dark_any = False
    for i in range(n):
        ti = rise + (set_ - rise) * i / (n - 1)
        try:
            az, el, rng, r = look.at(ti)
        except PropagationError:
            continue
        jd = jd_from_unix(ti)
        sun = _unit(sun_eci(jd))
        lit = in_sunlight(r, sun)
        sun_el = math.asin(max(-1.0, min(1.0, obs.up_dot(teme_to_ecef(sun, gstime(jd)))))) * RAD
        dark = sun_el <= twilight_deg
        sunlit_any = sunlit_any or lit
        dark_any = dark_any or dark
        if lit and dark and el >= min_el:
            if vis_start is None:
                vis_start = ti
            vis_end = ti
            vis_max_el = el if vis_max_el is None else max(vis_max_el, el)
            if std_mag is not None:
                # phase angle at the satellite between the Sun and the observer
                obs_eci = _ecef_to_eci(obs.ecef, gstime(jd))
                to_obs = (obs_eci[0] - r[0], obs_eci[1] - r[1], obs_eci[2] - r[2])
                u_obs = _unit(to_obs)
                cosphi = max(-1.0, min(1.0, sun[0] * u_obs[0] + sun[1] * u_obs[1] + sun[2] * u_obs[2]))
                m = magnitude(std_mag, rng, math.acos(cosphi))
                best_mag = m if best_mag is None else min(best_mag, m)
    out = {
        "rise": {"time": round(rise, 1), "az": round(az_r, 1), "dir": compass(az_r)},
        "max": {"time": round(t_max, 1), "el": round(el_max, 1), "az": round(az_m, 1), "dir": compass(az_m),
                "range_km": round(rng_m, 1)},
        "set": {"time": round(set_, 1), "az": round(az_s, 1), "dir": compass(az_s)},
        "duration_s": round(set_ - rise),
        "visible": vis_start is not None,
        "sunlit": sunlit_any,
        "observer_dark": dark_any,
        "in_progress": started,
        "cut_off": unfinished,
    }
    if vis_start is not None:
        out["visible_from"] = round(vis_start, 1)
        out["visible_to"] = round(vis_end, 1)
        out["visible_max_el"] = round(vis_max_el, 1)
        if best_mag is not None:
            out["magnitude"] = round(best_mag, 1)
    return out


def _ecef_to_eci(r, gmst):
    c = math.cos(gmst)
    s = math.sin(gmst)
    return (r[0] * c - r[1] * s, r[0] * s + r[1] * c, r[2])


def ground_track(s: Satrec, t_from: float, t_to: float, step_s: float = 60.0) -> list[tuple[float, float, float]]:
    """(t, lat, lon) samples between two times (decayed or failing samples are skipped)."""
    out = []
    t = t_from
    while t <= t_to + 1e-6:
        try:
            r, _v = propagate(s, t)
            lat, lon, _h = ecef_to_geodetic(teme_to_ecef(r, gstime(jd_from_unix(t))))
            out.append((t, lat, lon))
        except PropagationError:
            pass
        t += step_s
    return out
