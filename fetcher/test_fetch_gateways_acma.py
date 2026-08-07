"""Hermetic tests for the ACMA extractor against a synthetic RRL zip whose
columns follow the register's published table layout (create_tables.sql of
the public rrl_import project, cross-checked 2026-08-07)."""
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).parent


def make_zip(tmp: Path) -> Path:
    z = tmp / "spectra_rrl.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("client.csv",
            "CLIENT_NO,LICENCEE,TRADING_NAME\n"
            "101,SPACE EXPLORATION TECHNOLOGIES CORP,STARLINK\n"
            "102,TELSTRA LIMITED,\n"
            "103,AMAZON KUIPER SERVICES AUSTRALIA,\n")
        zf.writestr("licence.csv",
            "LICENCE_NO,CLIENT_NO,STATUS,STATUS_TEXT\n"
            "L1,101,GRA,Granted\n"
            "L2,102,GRA,Granted\n"
            "L3,103,GRA,Granted\n"
            "L4,101,EXP,Expired\n")
        # FREQUENCY in Hz (28.5 GHz uplink; 2 GHz; and one out-of-band 400 MHz
        # that must be dropped)
        zf.writestr("device_details.csv",
            "LICENCE_NO,SITE_ID,FREQUENCY,DEVICE_TYPE\n"
            "L1,S1,28500000000,T\n"
            "L1,S1,18500000000,R\n"
            "L2,S2,28000000000,T\n"       # non-operator licensee -> ignored
            "L3,S3,28100000000,T\n"
            "L1,S4,400000000,T\n"          # below band window -> dropped
            "L4,S5,29000000000,T\n")       # expired licence still reported
        zf.writestr("site.csv",
            "SITE_ID,LATITUDE,LONGITUDE,NAME,STATE,SITE_PRECISION,ELEVATION\n"
            "S1,-23.5320,133.8880,Alice Springs Earth Station,NT,Within 10 metres,545\n"
            "S2,-33.8000,151.2000,Telstra Sydney,NSW,Within 10 metres,20\n"
            "S3,-31.9500,115.8600,Perth Kuiper Site,WA,Within 100 metres,15\n"
            "S5,-35.3000,149.1000,Canberra Legacy,ACT,Within 10 metres,570\n")
        # S4 intentionally absent from site.csv: no coordinate -> no row
    return z


def run(tmp: Path, publish: bool):
    out = tmp / "gateways_acma.json"
    cmd = [sys.executable, str(HERE / "fetch_gateways_acma.py"),
           "--zip", str(tmp / "spectra_rrl.zip"), "--out", str(out)]
    if publish:
        cmd.append("--publish-ok")
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout
    return out


def test_extract_rows(tmp_path):
    make_zip(tmp_path)
    out = run(tmp_path, publish=True)
    doc = json.loads(out.read_text())
    rows = {(r["operator"], r["site_id"]): r for r in doc["rows"]}
    # Starlink Alice Springs present with register coordinates
    r = rows[("starlink", "S1")]
    assert abs(r["lat"] + 23.532) < 1e-6 and abs(r["lon"] - 133.888) < 1e-6
    assert r["n_tx_devices"] == 1 and r["n_rx_devices"] == 1
    assert r["provenance"] == "regulator_register"
    assert "L1" in r["licences"]
    # Kuiper site present
    assert ("kuiper", "S3") in rows
    # Non-operator licensee excluded
    assert not any(sid == "S2" for _, sid in rows)
    # Expired licence surfaces with its status visible, never silently dropped
    r5 = rows[("starlink", "S5")]
    assert "Expired" in r5["licence_status"]
    # Coordinate-less site produced no row (no geocoding, ever)
    assert not any(sid == "S4" for _, sid in rows)
    # Out-of-band 400 MHz device did not create a row on its own
    assert doc["n_rows"] == 3


def test_preview_withholds_rows(tmp_path):
    make_zip(tmp_path)
    out = run(tmp_path, publish=False)
    preview = out.with_suffix(".preview.json")
    assert preview.exists() and not out.exists()
    doc = json.loads(preview.read_text())
    assert "rows" not in doc
    assert doc["n_rows"] == 3          # counts visible, coordinates withheld
    assert "WITHHELD" in doc["publication"]


def test_aws_table_parser():
    from fetch_gateways_aws import parse_locations
    html = """<html><body><table>
    <tr><th>Ground Station Name</th><th>Ground Station Location</th>
        <th>AWS Region Name</th><th>AWS Region Code</th><th>Notes</th></tr>
    <tr><td>Ohio 1</td><td>Ohio, USA</td><td>US East (Ohio)</td>
        <td>us-east-2</td><td></td></tr>
    <tr><td>Dubbo 1</td><td>Dubbo, Australia</td><td>Asia Pacific (Sydney)</td>
        <td>ap-southeast-2</td><td>Not physically located in an AWS region</td></tr>
    </table></body></html>"""
    rows = parse_locations(html)
    assert len(rows) == 2
    assert rows[0]["station"] == "Ohio 1" and rows[0]["aws_region_code"] == "us-east-2"
    assert rows[1]["city"] == "Dubbo, Australia"
    assert "Not physically" in rows[1]["notes"]


def test_aws_wrong_table_yields_empty():
    from fetch_gateways_aws import parse_locations
    assert parse_locations("<table><tr><th>Other</th></tr><tr><td>x</td></tr></table>") == []
