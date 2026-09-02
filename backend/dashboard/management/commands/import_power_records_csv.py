"""Import verified PowerRecord CSV exports from another dashboard instance."""

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from dashboard.models import PowerRecord, SystemGroup


SOURCE_TIME_ZONE = ZoneInfo("Asia/Taipei")
SYSTEM_ID_PATTERN = re.compile(r"^system(?P<system_id>\d+)_", re.IGNORECASE)

CSV_FIELDS = {
    "PV電壓(V)": "voltage",
    "PV電流(A)": "current",
    "PV功率(W)": "power_output",
    "電池電壓(V)": "battery_voltage",
    "電池電流(A)": "battery_current",
    "電池功率(W)": "battery_power",
    "電池SOC(%)": "battery_soc",
    "樹莓派電壓(V)": "raspberry_pi_voltage",
    "樹莓派電流(mA)": "raspberry_pi_current",
    "樹莓派功率(W)": "raspberry_pi_power",
    "南北推桿角度(°)": "ns_actuator_angle",
    "南北推桿伸展(mm)": "ns_actuator_extension",
    "東西推桿角度(°)": "ew_actuator_angle",
    "東西推桿伸展(mm)": "ew_actuator_extension",
    "推桿總電壓(V)": "actuator_total_voltage",
    "推桿總電流(mA)": "actuator_total_current",
    "推桿總功率(W)": "actuator_total_power",
    "光照強度平均(lux)": "light_intensity",
    "北方LDR(lux)": "light_north",
    "東方LDR(lux)": "light_east",
    "西方LDR(lux)": "light_west",
    "南方LDR(lux)": "light_south",
    "面板傾角(°)": "panel_tilt",
    "面板方位角(°)": "panel_azimuth",
    "溫度(°C)": "temperature",
    "濕度(%)": "humidity",
}


def parse_float(value):
    value = (value or "").strip()
    return None if value == "" else float(value)


def parse_timestamp(value):
    clean = (value or "").lstrip("\ufeff").strip()
    naive = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
    return timezone.make_aware(naive, SOURCE_TIME_ZONE)


class Command(BaseCommand):
    help = "Import verified dashboard CSV exports without duplicating the local history"

    def add_arguments(self, parser):
        parser.add_argument("--manifest", required=True, help="Verified manifest.csv path")
        parser.add_argument(
            "--systems-json",
            help="Optional source SystemGroup API response used to preserve source IDs",
        )
        parser.add_argument(
            "--append-after-max",
            action="store_true",
            help="Only import rows later than each system's pre-import maximum timestamp",
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--batch-size", type=int, default=2000)

    def handle(self, *args, **options):
        manifest_path = Path(options["manifest"]).resolve()
        if not manifest_path.is_file():
            raise CommandError(f"Manifest not found: {manifest_path}")
        if options["batch_size"] < 1:
            raise CommandError("--batch-size must be at least 1")

        entries = self._read_manifest(manifest_path)
        system_ids = sorted({entry["system_id"] for entry in entries})
        cutoffs = {
            system_id: PowerRecord.objects.filter(system_id=system_id)
            .order_by("-timestamp")
            .values_list("timestamp", flat=True)
            .first()
            for system_id in system_ids
        }

        if options.get("systems_json"):
            systems_path = Path(options["systems_json"]).resolve()
            self._sync_systems(systems_path, dry_run=options["dry_run"])

        missing_systems = [
            system_id
            for system_id in system_ids
            if not SystemGroup.objects.filter(pk=system_id).exists()
        ]
        if missing_systems:
            raise CommandError(f"Missing SystemGroup IDs: {missing_systems}")

        totals = {"source": 0, "eligible": 0, "skipped": 0, "inserted": 0}
        for entry in entries:
            result = self._import_file(
                entry,
                cutoff=cutoffs[entry["system_id"]] if options["append_after_max"] else None,
                dry_run=options["dry_run"],
                batch_size=options["batch_size"],
            )
            for key in totals:
                totals[key] += result[key]
            self.stdout.write(
                f"{entry['path'].name}: source={result['source']} "
                f"eligible={result['eligible']} skipped={result['skipped']} "
                f"inserted={result['inserted']}"
            )

        mode = "DRY RUN" if options["dry_run"] else "COMPLETED"
        self.stdout.write(
            self.style.SUCCESS(
                f"{mode}: source={totals['source']} eligible={totals['eligible']} "
                f"skipped={totals['skipped']} inserted={totals['inserted']}"
            )
        )

    def _read_manifest(self, manifest_path):
        entries = []
        with manifest_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if (row.get("Match") or "").strip().lower() != "true":
                    raise CommandError(f"Manifest contains an unverified row: {row}")
                filename = (row.get("File") or "").strip()
                match = SYSTEM_ID_PATTERN.match(filename)
                if not match:
                    raise CommandError(f"Cannot determine system ID from filename: {filename}")
                path = manifest_path.parent / filename
                if not path.is_file():
                    raise CommandError(f"CSV not found: {path}")
                entries.append(
                    {
                        "path": path,
                        "system_id": int(match.group("system_id")),
                        "expected": int(row["Expected"]),
                    }
                )
        if not entries:
            raise CommandError("Manifest contains no importable rows")
        return entries

    def _sync_systems(self, systems_path, dry_run):
        if not systems_path.is_file():
            raise CommandError(f"Systems JSON not found: {systems_path}")
        payload = json.loads(systems_path.read_text(encoding="utf-8-sig"))
        systems = payload.get("results", payload)
        if not isinstance(systems, list):
            raise CommandError("Systems JSON must contain a list or a results list")

        for item in systems:
            system_id = int(item["id"])
            defaults = {
                "name": item["name"],
                "system_type": item["system_type"],
                "location": item["location"],
                "description": item.get("description", ""),
            }
            if not dry_run:
                SystemGroup.objects.update_or_create(pk=system_id, defaults=defaults)
            self.stdout.write(
                f"SystemGroup {system_id}: {defaults['name']}"
                + (" (dry run)" if dry_run else "")
            )

    @transaction.atomic
    def _import_file(self, entry, cutoff, dry_run, batch_size):
        source = eligible = skipped = inserted = 0
        batch = []
        system = SystemGroup.objects.get(pk=entry["system_id"])

        with entry["path"].open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [name for name in CSV_FIELDS if name not in (reader.fieldnames or [])]
            if missing:
                raise CommandError(f"{entry['path'].name} missing columns: {missing}")

            for row in reader:
                source += 1
                timestamp = parse_timestamp(row.get("時間戳(CST)"))
                if cutoff is not None and timestamp <= cutoff:
                    skipped += 1
                    continue

                eligible += 1
                values = {
                    model_field: parse_float(row.get(csv_field))
                    for csv_field, model_field in CSV_FIELDS.items()
                }
                if values["voltage"] is None or values["current"] is None:
                    raise CommandError(
                        f"{entry['path'].name} row {source + 1} is missing voltage/current"
                    )
                if values["power_output"] is None:
                    values["power_output"] = values["voltage"] * values["current"]

                batch.append(
                    PowerRecord(
                        system=system,
                        timestamp=timestamp,
                        notes=(row.get("備註") or "")[:200],
                        **values,
                    )
                )
                if len(batch) >= batch_size:
                    if not dry_run:
                        PowerRecord.objects.bulk_create(batch, batch_size=batch_size)
                        inserted += len(batch)
                    batch.clear()

        if source != entry["expected"]:
            raise CommandError(
                f"{entry['path'].name}: expected {entry['expected']} rows, found {source}"
            )
        if batch and not dry_run:
            PowerRecord.objects.bulk_create(batch, batch_size=batch_size)
            inserted += len(batch)

        return {
            "source": source,
            "eligible": eligible,
            "skipped": skipped,
            "inserted": inserted,
        }
