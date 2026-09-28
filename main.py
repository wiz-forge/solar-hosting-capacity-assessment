"""
================================================================================
 DISTRIBUTION FEEDER SOLAR HOSTING CAPACITY ASSESSMENT
================================================================================

Author  : wiz-forge
Purpose : Portfolio project demonstrating distribution planning competency -
          power flow analysis and rooftop-solar hosting capacity screening
          on a radial distribution feeder.

--------------------------------------------------------------------------------
ENGINEERING LOGIC
--------------------------------------------------------------------------------
1. A radial distribution feeder is modeled and solved for its BASE CASE
   (no solar) power flow. Voltage magnitudes (per-unit) are extracted at
   every bus.
2. The three buses with the LOWEST base-case voltage are identified as the
   "electrically weakest" points on the feeder - typically the buses
   electrically farthest from the substation and most sensitive to voltage
   rise caused by reverse power flow from distributed solar PV.
3. Rooftop solar PV is modeled as a simple negative PQ load (a standard
   simplification for rapid hosting-capacity screening studies) injected
   at the three weak buses.
4. The penetration sweep runs from 0% to 400% of local load. Levels above
   100% represent midday EXPORT conditions, where rooftop PV generation
   exceeds local demand and reverse power flow raises feeder voltage -
   the primary overvoltage mechanism faced by Indian distribution utilities.
5. At each step the power flow is re-solved and the full bus voltage profile
   is recorded, identifying the penetration level at which the statutory
   upper voltage limit (1.05 pu) is first violated.

--------------------------------------------------------------------------------
MODEL VALIDATION NOTE (IMPORTANT - READ BEFORE MODIFYING BACKENDS)
--------------------------------------------------------------------------------
Two independent power flow backends were implemented during development:
OpenDSS (via opendssdirect.py) and pandapower. Both were built to represent
the SAME feeder topology, impedances, and loading (see NETWORK SPECIFICATION
below) to allow direct cross-validation.

A physical sanity check was applied: at exactly 100% solar penetration, real
power (P) injected by solar exactly cancels the local real load (net P ~ 0),
leaving only the pre-existing inductive reactive load (Q) at that bus. Basic
distribution voltage-drop theory (dV ~ (P.R + Q.X)/V^2) dictates that with
P~0, voltage should remain slightly BELOW the source reference (1.0 pu) -
it should NOT exceed it.

- pandapower result at 100% penetration: ~0.988 pu (below 1.0)  -> PASSES check
- OpenDSS result at 100% penetration:     ~1.055 pu (above 1.0) -> FAILS check

This indicates an unresolved implementation issue in the OpenDSS solar-load
injection path (likely related to load connection/kV-base interaction), not
a genuine physical phenomenon. Rather than report unvalidated numbers, this
script uses PANDAPOWER as the authoritative backend for all reported results.
The OpenDSS implementation is retained in the codebase for transparency,
reference, and future debugging, but is disabled by default via the
USE_OPENDSS flag below.
================================================================================
"""

import re
import warnings

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

warnings.filterwarnings("ignore")

# ------------------------------------------------------------------------
# OPTIONAL DEPENDENCY IMPORTS (handled gracefully if not installed)
# ------------------------------------------------------------------------
try:
    import opendssdirect as dss
    OPENDSS_AVAILABLE = True
except ImportError:
    OPENDSS_AVAILABLE = False

try:
    import pandapower as pp
    PANDAPOWER_AVAILABLE = True
except ImportError:
    PANDAPOWER_AVAILABLE = False


# ============================================================================
# CONFIGURATION
# ============================================================================

# NOTE: OpenDSS backend is implemented and available below, but is DISABLED
# by default for official results due to the unresolved validation
# discrepancy documented above. Set to True only after the OpenDSS
# solar-injection bug has been isolated and re-validated against the same
# zero-net-power sanity check described above.
USE_OPENDSS = False

VOLTAGE_UPPER_LIMIT = 1.05  # pu - standard statutory upper limit
VOLTAGE_LOWER_LIMIT = 0.95  # pu - standard statutory lower limit

# ----------------------------------------------------------------------
# SHARED NETWORK SPECIFICATION
# Both backends are built from these SAME parameters to ensure consistent,
# comparable, and cross-checkable results.
# ----------------------------------------------------------------------
N_BUSES = 13
VOLTAGE_KV = 4.16                        # feeder nominal line-line voltage (kV)
SEGMENT_LENGTH_KM = 0.25                 # length of each line segment (km)
R_OHM_PER_KM = 0.15                      # positive-sequence resistance (ohm/km)
X_OHM_PER_KM = 0.12                      # positive-sequence reactance (ohm/km)
LOAD_KW = 500.0                          # real power per loaded bus (kW)
LOAD_KVAR = 200.0                        # reactive power per loaded bus (kvar)
LOAD_BUSES = [11, 12, 13]                # buses carrying local load

# Maps penetration percentage -> output DataFrame column name.
# Sweep extends to 400% to capture midday PV export conditions and to
# ensure the hosting-capacity violation threshold is actually reached.
PCT_COLUMN_MAP = {
    0: "Base_Voltage_pu",
    50: "V_50_pct",
    100: "V_100_pct",
    150: "V_150_pct",
    200: "V_200_pct",
    250: "V_250_pct",
    300: "V_300_pct",
    350: "V_350_pct",
    400: "V_400_pct",
}
PENETRATION_LEVELS = sorted(PCT_COLUMN_MAP.keys())

# Penetration level used for the side-by-side profile comparison chart
PROFILE_COMPARE_PCT = 300


# ============================================================================
# UTILITY: NATURAL SORT (fixes "Bus10 before Bus2" alphabetical sort issue)
# ============================================================================
def natural_sort_key(s):
    """Sort key that orders embedded numbers numerically, not lexically."""
    return [int(chunk) if chunk.isdigit() else chunk.lower()
            for chunk in re.split(r"(\d+)", str(s))]


def natural_sort_dataframe(df, column):
    """Returns df sorted by `column` using natural (numeric-aware) order."""
    return df.iloc[df[column].map(natural_sort_key).argsort()].reset_index(drop=True)


# ============================================================================
# BACKEND 1: OPENDSS (RETAINED FOR REFERENCE - DISABLED BY DEFAULT)
# ============================================================================
class OpenDSSBackend:
    """
    Builds a simplified radial distribution feeder directly in OpenDSS via
    dss.run_command() - no external files or downloads required. Uses the
    SAME topology/impedance/load parameters as the pandapower backend (see
    NETWORK SPECIFICATION above) to allow direct cross-validation.

    STATUS: Implemented and functional (compiles and solves), but produces
    results that FAIL the zero-net-power physical sanity check described
    in the module docstring. Retained here for transparency and future
    debugging. NOT used for official results unless USE_OPENDSS = True.

    API NOTE: Uses dss.run_command() rather than dss.Text() or
    dss.Text.Command(), as this is the most version-stable calling
    convention across different opendssdirect.py releases.
    """

    def __init__(self):
        self._solar_loads = []

    def build_and_compile(self):
        """Programmatically constructs and solves the feeder in OpenDSS."""
        dss.run_command("Clear")
        dss.run_command(f"New Circuit.SimpleFeeder basekv={VOLTAGE_KV} "
                         f"pu=1.0 phases=3 bus1=Bus1")

        dss.run_command(f"New LineCode.LC1 nphases=3 units=km "
                         f"R1={R_OHM_PER_KM} X1={X_OHM_PER_KM} "
                         f"R0={R_OHM_PER_KM} X0={X_OHM_PER_KM}")

        for i in range(1, N_BUSES):
            dss.run_command(f"New Line.Line{i} Bus1=Bus{i} Bus2=Bus{i + 1} "
                             f"LineCode=LC1 Length={SEGMENT_LENGTH_KM} "
                             f"units=km Phases=3")

        for b in LOAD_BUSES:
            dss.run_command(f"New Load.Load_Bus{b} Bus1=Bus{b} Phases=3 "
                             f"Conn=Wye Model=1 kV={VOLTAGE_KV} "
                             f"kW={LOAD_KW} kvar={LOAD_KVAR}")

        dss.run_command("Set VoltageBases=[4.16]")
        dss.run_command("CalcVoltageBases")
        dss.run_command("Set Mode=Snapshot")

        return dss.Circuit.NumBuses() > 0

    def solve(self):
        dss.Solution.Solve()
        return dss.Solution.Converged()

    def get_voltages(self):
        """Returns per-bus voltage magnitude (p.u.) as a pandas Series."""
        node_names = dss.Circuit.AllNodeNames()
        v_mag_pu = (dss.Circuit.AllBusMagPu() if hasattr(dss.Circuit, "AllBusMagPu")
                    else dss.Circuit.AllBusVmagPu())
        df = pd.DataFrame({"node": node_names, "vpu": v_mag_pu})
        df["bus"] = df["node"].apply(lambda x: x.split(".")[0])
        return df.groupby("bus")["vpu"].mean()

    def _get_bus_phases(self, bus_name):
        node_names = dss.Circuit.AllNodeNames()
        phases = [n.split(".")[1] for n in node_names
                  if n.split(".")[0].lower() == bus_name.lower()]
        return sorted(set(phases)) or ["1", "2", "3"]

    def _get_bus_kv_ln(self, bus_name):
        dss.Circuit.SetActiveBus(bus_name)
        return dss.Bus.kVBase()

    def get_bus_load_kw(self, bus_name):
        total_kw = 0.0
        idx = dss.Loads.First()
        while idx:
            bus1 = dss.CktElement.BusNames()[0].split(".")[0]
            if bus1.lower() == bus_name.lower():
                total_kw += dss.Loads.kW()
            idx = dss.Loads.Next()
        return total_kw

    def add_solar_load(self, bus_name, kw):
        """Adds/updates rooftop-solar as a negative PQ load at bus_name."""
        name = f"Solar_{bus_name}"
        if name not in self._solar_loads:
            phases = self._get_bus_phases(bus_name)
            n_ph = max(len(phases), 1)
            bus_conn = f"{bus_name}." + ".".join(phases)
            kv_ln = self._get_bus_kv_ln(bus_name)
            dss.run_command(f"New Load.{name} Bus1={bus_conn} Phases={n_ph} "
                             f"Conn=Wye Model=1 kV={kv_ln:.4f} "
                             f"kW={-abs(kw):.4f} kvar=0")
            self._solar_loads.append(name)
        else:
            dss.run_command(f"Edit Load.{name} kW={-abs(kw):.4f}")


# ============================================================================
# BACKEND 2: PANDAPOWER (VALIDATED - USED FOR ALL OFFICIAL RESULTS)
# ============================================================================
class PandapowerBackend:
    """
    Builds the SAME feeder topology/parameters as OpenDSSBackend using
    pandapower. This backend PASSED the zero-net-power physical sanity
    check (see module docstring) and is used as the authoritative source
    for all results reported by this study.
    """

    def __init__(self):
        self.net = None
        self.bus_idx = {}
        self._solar_load_idx = {}

    def build_synthetic_feeder(self):
        net = pp.create_empty_network(f_hz=50.0, name="Synthetic_13Bus_Feeder")

        bus_idx = {}
        for i in range(1, N_BUSES + 1):
            bus_idx[f"Bus{i}"] = pp.create_bus(net, vn_kv=VOLTAGE_KV, name=f"Bus{i}")

        pp.create_ext_grid(net, bus=bus_idx["Bus1"], vm_pu=1.00, name="Substation")

        for i in range(1, N_BUSES):
            pp.create_line_from_parameters(
                net,
                from_bus=bus_idx[f"Bus{i}"],
                to_bus=bus_idx[f"Bus{i + 1}"],
                length_km=SEGMENT_LENGTH_KM,
                r_ohm_per_km=R_OHM_PER_KM,
                x_ohm_per_km=X_OHM_PER_KM,
                c_nf_per_km=10.0,
                max_i_ka=0.4,
                name=f"Line_{i}_{i + 1}",
            )

        for b in LOAD_BUSES:
            pp.create_load(net, bus=bus_idx[f"Bus{b}"],
                            p_mw=LOAD_KW / 1000.0, q_mvar=LOAD_KVAR / 1000.0,
                            name=f"Load_Bus{b}")

        self.net = net
        self.bus_idx = bus_idx
        return net

    def solve(self):
        try:
            pp.runpp(self.net, algorithm="nr", init="auto", numba=False)
            return bool(self.net["converged"])
        except Exception:
            return False

    def get_voltages(self):
        res = self.net.res_bus.copy()
        res["bus_name"] = self.net.bus["name"].values
        return res.set_index("bus_name")["vm_pu"]

    def get_bus_load_kw(self, bus_name):
        b = self.bus_idx[bus_name]
        loads = self.net.load[self.net.load.bus == b]
        return loads["p_mw"].sum() * 1000.0

    def add_solar_load(self, bus_name, kw):
        b = self.bus_idx[bus_name]
        p_mw = -abs(kw) / 1000.0
        if bus_name in self._solar_load_idx:
            idx = self._solar_load_idx[bus_name]
            self.net.load.at[idx, "p_mw"] = p_mw
        else:
            idx = pp.create_load(self.net, bus=b, p_mw=p_mw, q_mvar=0.0,
                                  name=f"Solar_{bus_name}")
            self._solar_load_idx[bus_name] = idx


# ============================================================================
# STEP 1: NETWORK SETUP
# ============================================================================
def setup_network():
    """
    Sets up the feeder model. By default, uses the VALIDATED pandapower
    backend (see module docstring for the validation rationale). The
    OpenDSS backend can be re-enabled via the USE_OPENDSS flag once its
    solar-injection discrepancy has been resolved and re-validated.
    """
    print("=" * 78)
    print("STEP 1: NETWORK SETUP")
    print("=" * 78)

    if USE_OPENDSS and OPENDSS_AVAILABLE:
        print("Building 13-bus radial feeder programmatically in OpenDSS...")
        try:
            backend = OpenDSSBackend()
            if backend.build_and_compile() and backend.solve():
                print("  [SUCCESS] OpenDSS feeder built, compiled and solved.")
                print("  [CAUTION] OpenDSS backend has NOT passed validation "
                      "- see module docstring.")
                return backend, "OpenDSS - Programmatically Built 13-Bus Feeder (UNVALIDATED)"
            print("  [WARN] Model built but did not converge.")
        except Exception as exc:
            print(f"  [WARN] OpenDSS build/solve error: {exc}")
        print("Falling back to validated pandapower backend...\n")
    else:
        print("Using pandapower backend (validated - see module docstring "
              "for cross-validation details).")

    if not PANDAPOWER_AVAILABLE:
        raise EnvironmentError(
            "pandapower is required for this study. Install it via:\n"
            "  pip install pandapower"
        )

    backend = PandapowerBackend()
    backend.build_synthetic_feeder()
    if backend.solve():
        print("  [SUCCESS] pandapower feeder built and solved.")
        return backend, "pandapower - Programmatically Built 13-Bus Feeder (Validated)"

    raise RuntimeError("Pandapower network failed to converge. Aborting.")


# ============================================================================
# STEP 2: BASE CASE POWER FLOW
# ============================================================================
def run_base_case(backend):
    """
    Solves the base-case (no solar) power flow and identifies the three
    electrically weakest (lowest voltage) buses on the feeder.
    """
    print("\n" + "=" * 78)
    print("STEP 2: BASE CASE POWER FLOW")
    print("=" * 78)

    if not backend.solve():
        raise RuntimeError("Base case power flow did not converge.")

    voltages = backend.get_voltages().sort_values()
    weak_buses = voltages.index[:3].tolist()

    print(f"Base case converged. {len(voltages)} buses analyzed.")
    print(f"Three electrically weakest buses (lowest base voltage): {weak_buses}")
    for b in weak_buses:
        print(f"    {b}: {voltages[b]:.4f} pu")

    violations = voltages[(voltages < VOLTAGE_LOWER_LIMIT) |
                           (voltages > VOLTAGE_UPPER_LIMIT)]
    if len(violations) > 0:
        print(f"[WARNING] Base case has {len(violations)} pre-existing "
              f"voltage violation(s) - check network parameters.")
    else:
        print(f"[OK] No voltage violations in base case "
              f"(all buses within {VOLTAGE_LOWER_LIMIT}-{VOLTAGE_UPPER_LIMIT} pu).")

    return voltages, weak_buses


# ============================================================================
# STEP 3: SOLAR PENETRATION SWEEP
# ============================================================================
def run_solar_sweep(backend, weak_buses, penetration_levels=PENETRATION_LEVELS):
    """
    Performs a rooftop-solar penetration sweep at the identified weak
    buses, re-solving the power flow at each step and recording the
    resulting voltage profile across the entire feeder.
    """
    print("\n" + "=" * 78)
    print(f"STEP 3: SOLAR PENETRATION SWEEP "
          f"(0-{max(penetration_levels)}% of local load)")
    print("=" * 78)

    base_loads_kw = {b: backend.get_bus_load_kw(b) for b in weak_buses}
    print("Existing local load at weak buses (used as 100% PV reference):")
    for b, kw in base_loads_kw.items():
        print(f"    {b}: {kw:.2f} kW")
    print("Note: penetration >100% represents midday PV export conditions.\n")

    results = {}
    print("Running penetration sweep...")
    for pct in penetration_levels:
        if pct > 0:
            for b in weak_buses:
                pv_kw = base_loads_kw[b] * (pct / 100.0)
                backend.add_solar_load(b, pv_kw)

        if not backend.solve():
            print(f"  [WARN] Power flow did not converge at {pct}% penetration.")

        voltages = backend.get_voltages()
        results[pct] = voltages
        max_v = voltages.max()
        status = "VIOLATION" if max_v > VOLTAGE_UPPER_LIMIT else "OK"
        print(f"  Penetration {pct:>3d}% | Max feeder voltage: {max_v:.4f} pu "
              f"| Status: {status}")

    return results


# ============================================================================
# DATA PROCESSING
# ============================================================================
def build_results_dataframe(results):
    """
    Consolidates the penetration sweep results into a single, tidy
    Pandas DataFrame, ordered using a NATURAL (numeric-aware) sort on
    bus name.

    Columns: Bus_Name, Base_Voltage_pu, V_50_pct, V_100_pct, ..., V_400_pct
    """
    df = pd.DataFrame({"Bus_Name": results[0].index})
    for pct, colname in PCT_COLUMN_MAP.items():
        df[colname] = df["Bus_Name"].map(results[pct])

    return natural_sort_dataframe(df, "Bus_Name")


# ============================================================================
# STEP 4: VISUALIZATIONS
# ============================================================================
def generate_plots(df, weak_buses):
    """
    Generates two recruiter-friendly visualizations and saves them as
    high-resolution PNG files:
      1. Base case vs. PROFILE_COMPARE_PCT solar penetration voltage
         profile (all buses)
      2. Voltage trajectory at the three weakest buses across the full
         0-400% penetration sweep, with the violation zone shaded
    """
    print("\n" + "=" * 78)
    print("STEP 4: GENERATING VISUALIZATIONS")
    print("=" * 78)

    df_sorted = natural_sort_dataframe(df, "Bus_Name")
    compare_col = PCT_COLUMN_MAP[PROFILE_COMPARE_PCT]

    # ---------------- PLOT 1: Base vs. Export-Condition Solar Profile -------
    fig, ax = plt.subplots(figsize=(12, 6))
    x = np.arange(len(df_sorted))
    width = 0.35

    ax.bar(x - width / 2, df_sorted["Base_Voltage_pu"], width,
           label="Base Case (0% Solar)", color="#4C72B0")
    ax.bar(x + width / 2, df_sorted[compare_col], width,
           label=f"{PROFILE_COMPARE_PCT}% Solar Penetration (Export)",
           color="#DD8452")

    ax.axhline(VOLTAGE_UPPER_LIMIT, color="red", linestyle="--", linewidth=1.5,
               label=f"Upper Limit ({VOLTAGE_UPPER_LIMIT} pu)")
    ax.axhline(VOLTAGE_LOWER_LIMIT, color="orange", linestyle="--", linewidth=1.5,
               label=f"Lower Limit ({VOLTAGE_LOWER_LIMIT} pu)")

    xtick_labels = [f"*{b}*" if b in weak_buses else b
                    for b in df_sorted["Bus_Name"]]
    ax.set_xticks(x)
    ax.set_xticklabels(xtick_labels, rotation=45, ha="right")
    ax.set_ylabel("Voltage (p.u.)")
    ax.set_ylim(0.93, 1.08)
    ax.set_title("Distribution Feeder Voltage Profile:\n"
                  f"Base Case vs. {PROFILE_COMPARE_PCT}% Solar Injection "
                  "(* = electrically weakest buses)")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=2, framealpha=0.9)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig("plot1_voltage_profile.png", dpi=300, bbox_inches="tight")
    plt.close(fig)
    print("  [SAVED] plot1_voltage_profile.png")

    # ---------------- PLOT 2: Weak Bus Voltage Trajectory --------------------
    fig2, ax2 = plt.subplots(figsize=(10, 6))
    for b in weak_buses:
        row = df[df["Bus_Name"] == b].iloc[0]
        v_trajectory = [row[PCT_COLUMN_MAP[p]] for p in PENETRATION_LEVELS]
        ax2.plot(PENETRATION_LEVELS, v_trajectory, marker="o", linewidth=2, label=b)

    y_top = max(1.12, df[[PCT_COLUMN_MAP[p] for p in PENETRATION_LEVELS]].values.max() + 0.01)
    ax2.axhline(VOLTAGE_UPPER_LIMIT, color="red", linestyle="--", linewidth=1.5,
                label=f"Upper Limit ({VOLTAGE_UPPER_LIMIT} pu)")
    ax2.fill_between(PENETRATION_LEVELS, VOLTAGE_UPPER_LIMIT, y_top,
                      color="red", alpha=0.08, label="Violation Zone")
    ax2.set_xlabel("Solar Penetration at Weak Buses (% of Local Load)")
    ax2.set_ylabel("Voltage (p.u.)")
    ax2.set_title("Voltage Rise at Electrically Weak Buses vs. Solar Penetration")
    ax2.legend(loc="upper left")
    ax2.grid(alpha=0.3)
    fig2.tight_layout()
    fig2.savefig("plot2_weak_buses.png", dpi=300)
    plt.close(fig2)
    print("  [SAVED] plot2_weak_buses.png")


# ============================================================================
# STEP 5: EXECUTIVE SUMMARY
# ============================================================================
def print_summary(df, weak_buses, network_label):
    """
    Prints a concise, recruiter-friendly executive summary of the study
    findings, including the base case status, the penetration level at
    which the first voltage violation occurs, and a business/technical
    conclusion.
    """
    print("\n" + "=" * 78)
    print("EXECUTIVE SUMMARY")
    print("=" * 78)
    print(f"Network Model Used       : {network_label}")
    print(f"Total Buses Analyzed     : {len(df)}")
    print(f"Weakest Buses Identified : {', '.join(weak_buses)}")

    base_violations = df[(df["Base_Voltage_pu"] < VOLTAGE_LOWER_LIMIT) |
                          (df["Base_Voltage_pu"] > VOLTAGE_UPPER_LIMIT)]
    print(f"Base Case Status         : "
          f"{'VIOLATIONS FOUND' if len(base_violations) > 0 else 'HEALTHY (no violations)'}")

    first_violation_pct = None
    for pct in PENETRATION_LEVELS:
        if df[PCT_COLUMN_MAP[pct]].max() > VOLTAGE_UPPER_LIMIT:
            first_violation_pct = pct
            break

    if first_violation_pct is not None:
        col = PCT_COLUMN_MAP[first_violation_pct]
        offending_bus = df.loc[df[col].idxmax(), "Bus_Name"]
        max_v = df[col].max()
        prior_pct = PENETRATION_LEVELS[max(PENETRATION_LEVELS.index(first_violation_pct) - 1, 0)]
        print(f"First Voltage Violation  : At {first_violation_pct}% solar penetration "
              f"(Bus {offending_bus} reaches {max_v:.4f} pu)")
        conclusion = (
            f"Estimated hosting capacity for this feeder is between "
            f"{prior_pct}% and {first_violation_pct}% of connected local load "
            f"at its weakest buses. Beyond this threshold, midday PV export "
            f"drives voltages above the 1.05 pu statutory limit; voltage "
            f"regulation equipment (smart inverters with Volt-VAR control, "
            f"capacitor banks, or OLTC adjustment) would be required before "
            f"approving further rooftop solar interconnection requests."
        )
    else:
        print(f"First Voltage Violation  : None observed up to "
              f"{max(PENETRATION_LEVELS)}% penetration.")
        conclusion = (
            f"This feeder can accommodate up to {max(PENETRATION_LEVELS)}% "
            f"local-load-equivalent solar penetration at its weakest buses "
            f"without voltage violations, indicating healthy headroom for "
            f"continued rooftop solar interconnection approvals."
        )

    print("\nCONCLUSION:")
    print(f"  {conclusion}")
    print("=" * 78)


# ============================================================================
# MAIN EXECUTION
# ============================================================================
def main():
    print("\n" + "#" * 78)
    print("# DISTRIBUTION FEEDER SOLAR HOSTING CAPACITY ASSESSMENT")
    print("#" * 78)

    backend, network_label = setup_network()
    voltages, weak_buses = run_base_case(backend)
    results = run_solar_sweep(backend, weak_buses)
    df = build_results_dataframe(results)

    print("\n" + "=" * 78)
    print("CONSOLIDATED RESULTS TABLE")
    print("=" * 78)
    print(df.to_string(index=False))

    df.to_csv("solar_hosting_capacity_results.csv", index=False)
    print("\n  [SAVED] solar_hosting_capacity_results.csv")

    generate_plots(df, weak_buses)
    print_summary(df, weak_buses, network_label)


if __name__ == "__main__":
    main()