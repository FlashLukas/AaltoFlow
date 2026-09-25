"""The real cryostat: a Quantum Design DynaCool, through MultiVu and MultiPyVu.

This is the ONLY file that touches Quantum Design's software, and it imports
MultiPyVu inside `open()`, so the package (and every test) works on a PC without
it. Install on the DynaCool PC with  `uv sync --extra gui --extra real`.

How it reaches the instrument, and why this way:

  MultiVu (QD's own program) owns the DynaCool. Nothing talks to the hardware
  directly; you ask MultiVu. Quantum Design's supported Python route is
  MultiPyVu, which is itself TWO pieces: a small socket SERVER that sits next to
  MultiVu and talks to it over Windows COM (pywin32), and a CLIENT that sends it
  commands. The old LabVIEW program used the older .NET equivalent
  (QDInstrument.dll + QDInstrument_Server.exe).

  Everything runs on ONE PC here (MultiVu, the VNA software and this service),
  so this backend starts the MultiPyVu server INSIDE this process, bound to
  127.0.0.1 only, and connects to it as its one client. Nothing else to launch,
  nothing listening on the network. (MultiPyVu's server would otherwise bind
  0.0.0.0 -- every network interface -- which is not what a lab PC needs.)

Units: MultiVu speaks OERSTED (and Oe/s); the suite speaks mT. 1 mT = 10 Oe
(mu0*H: 1 Oe <-> 0.1 mT). The conversion happens HERE and nowhere else, which
is exactly what the old program did by dividing GET FIELD by 10. Temperature is
K and K/min on both sides.

UNTESTED ON THE INSTRUMENT. Written against MultiPyVu 3.6.1's source; the lines
that need a look on the real DynaCool are marked `# VERIFY`. The whole path can
be exercised WITHOUT the cryostat in MultiPyVu's own simulation
("scaffolding", `hardware.scaffolding = True` or `run_service.py --real
--scaffold`), which needs neither MultiVu nor pywin32.
"""

from __future__ import annotations

OE_PER_MT = 10.0          # 1 mT (mu0*H) = 10 Oe


class MultiVuDynaCool:
    simulated = False

    def __init__(self, cfg, mpv=None):
        """`cfg` = the module Config. `mpv` = the MultiPyVu module; tests pass a
        FAKE one, normally it is None and `open()` imports the real package."""
        self.cfg = cfg
        self._mpv = mpv
        self._server = None
        self._client = None
        self._idn = ""

    # ---- lifecycle -------------------------------------------------------------

    def open(self) -> None:
        hw = self.cfg.hardware
        mpv = self._mpv
        if mpv is None:
            try:
                import MultiPyVu as mpv            # lazy: only the real backend needs it
            except ImportError as exc:
                raise RuntimeError(
                    "MultiPyVu is not installed. In ppms-control run: "
                    "uv sync --extra gui --extra real") from exc
            self._mpv = mpv

        flags = []
        if hw.flavor.strip():
            flags.append(hw.flavor.strip().upper())
        if hw.scaffolding:
            flags.append("-s")
        port = int(hw.mpv_port)
        try:
            # MultiPyVu's Server calls sys.exit() when it cannot attach to
            # MultiVu (wrong flavor, MultiVu not running, no pywin32). Inside a
            # service that would silently end the whole process, so turn it
            # into an ordinary error the service can report.
            self._server = mpv.Server(flags, host="127.0.0.1", port=port)
            self._server.open()                    # runs in its own thread
        except SystemExit as exc:
            self._server = None
            raise RuntimeError(
                "MultiPyVu could not attach to MultiVu. Is MultiVu running on "
                f"this PC, and is it a {hw.flavor or 'QD'} MultiVu? ({exc})") from None
        try:
            self._client = mpv.Client(host="127.0.0.1", port=port)
            self._client.open()
        except BaseException:
            self._stop_server()
            raise
        name = getattr(self._client, "instrument_name", "") or hw.flavor
        version = getattr(mpv, "__version__", "?")
        mode = ", simulated by MultiPyVu" if hw.scaffolding else ""
        self._idn = f"{name} (MultiPyVu {version}{mode})"

    def close(self) -> None:
        """Disconnect WITHOUT touching field or temperature: a cryostat left at
        5 T and 2 K on purpose must stay there when the service stops."""
        c, self._client = self._client, None
        if c is not None:
            try:
                c.close_client()
            except BaseException:
                pass                               # closing must not raise on a dead link
        self._stop_server()

    def _stop_server(self) -> None:
        s, self._server = self._server, None
        if s is not None:
            try:
                s.close()
            except BaseException:
                pass

    def idn(self) -> str:
        return self._idn

    # ---- field ------------------------------------------------------------------

    def read_field(self) -> tuple[float, str]:
        # MultiPyVu returns (Oe, status). On an internal error its own error
        # path can raise instead of returning; the brain's poll loop reports
        # any exception as hw_error, so nothing is caught here.
        h_Oe, status = self._c().get_field()
        return float(h_Oe) / OE_PER_MT, str(status)

    def read_field_setpoint(self) -> tuple[float, float, str]:
        # (Oe, Oe/s, approach name, driven-mode name)
        f, rate, approach, _driven = self._c().get_field_setpoints()
        return float(f) / OE_PER_MT, float(rate) / OE_PER_MT, str(approach)

    def set_field(self, field_mT: float, rate_mT_per_s: float, approach: str) -> None:
        c = self._c()
        mode = c.field.approach_mode[approach]     # KeyError -> "bad request"
        # driven_mode is left out: it is only for the classic PPMS, and the
        # DynaCool magnet is always driven.
        # VERIFY on the DynaCool: MultiVu's accepted rate range, and that a
        # setpoint beyond the magnet's rating is refused rather than clipped.
        c.set_field(float(field_mT) * OE_PER_MT, float(rate_mT_per_s) * OE_PER_MT, mode)

    # ---- temperature ------------------------------------------------------------

    def read_temperature(self) -> tuple[float, str]:
        t, status = self._c().get_temperature()
        return float(t), str(status)

    def read_temperature_setpoint(self) -> tuple[float, float, str]:
        t, rate, approach = self._c().get_temperature_setpoints()
        return float(t), float(rate), str(approach)

    def set_temperature(self, temperature_K: float, rate_K_per_min: float,
                        approach: str) -> None:
        c = self._c()
        mode = c.temperature.approach_mode[approach]
        c.set_temperature(float(temperature_K), float(rate_K_per_min), mode)

    # ---- chamber ------------------------------------------------------------------

    def read_chamber(self) -> str:
        return str(self._c().get_chamber())

    # ---- internals ----------------------------------------------------------------

    def _c(self):
        if self._client is None:
            raise RuntimeError("MultiVu is not connected")
        return self._client
