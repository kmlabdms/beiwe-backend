import hashlib
import sys
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from django.db.models import Max, Min

from constants.common_constants import CHUNKS_FOLDER
from constants.data_processing_constants import CHUNK_TIMESLICE_QUANTUM
from constants.data_stream_constants import ALL_DATA_STREAMS_SET, CHUNKABLE_FILES
from constants.user_constants import ANDROID_API, IOS_API
from database.data_access_models import ChunkRegistry
from database.models import Participant, Study
from libs.file_processing.data_qty_stats import calculate_data_quantity_stats
from libs.s3 import s3_upload, s3_upload_no_compression
from libs.utils.compression import compress
from libs.utils.security_utils import chunk_hash as compute_chunk_hash


class Command(BaseCommand):
    help = (
        "Import pre-processed Beiwe CSV data (e.g. a Zenodo public dataset) into a study. "
        "Source directory must be structured as <data_dir>/<patient_id>/<data_stream>/<timestamp>.csv, "
        "which is the format produced by the Beiwe data download pipeline."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "study",
            help="Study name or 24-character object_id to import data into.",
        )
        parser.add_argument(
            "data_dir",
            help="Path to the directory containing participant subdirectories.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be imported without writing anything.",
        )

    def handle(self, *args, **options):
        data_path = Path(options["data_dir"])
        if not data_path.is_dir():
            raise CommandError(f"'{options['data_dir']}' is not a directory.")

        study = self._get_study(options["study"])
        dry_run = options["dry_run"]

        self.stdout.write(f"Study: {study.name} ({study.object_id})")
        if dry_run:
            self.stdout.write("Dry-run mode — nothing will be written.\n")

        counts = {"ok": 0, "skip": 0, "error": 0}

        for patient_dir in sorted(data_path.iterdir()):
            if not patient_dir.is_dir() or patient_dir.name.startswith("."):
                continue

            patient_id = patient_dir.name
            os_type = self._infer_os_type(patient_dir)

            if dry_run:
                participant = None
                self.stdout.write(f"\nParticipant {patient_id} (os={os_type or 'unknown'})")
            else:
                participant, created = self._get_or_create_participant(patient_id, study, os_type)
                label = "Created" if created else "Found"
                self.stdout.write(f"\n{label} participant {patient_id} (os={os_type or 'unknown'})")

            for stream_dir in sorted(patient_dir.iterdir()):
                if not stream_dir.is_dir():
                    continue

                data_type = stream_dir.name
                if data_type not in ALL_DATA_STREAMS_SET:
                    self.stdout.write(f"  Skipping unknown stream '{data_type}'")
                    continue

                csv_files = sorted(stream_dir.glob("*.csv"))
                self.stdout.write(f"  {data_type}: {len(csv_files)} file(s)")

                for csv_path in csv_files:
                    try:
                        status = self._import_file(csv_path, data_type, participant, study, dry_run)
                        if status.startswith(("ok", "dry-run")):
                            counts["ok"] += 1
                        else:
                            counts["skip"] += 1
                        self.stdout.write(f"    {csv_path.name}: {status}")
                    except Exception as e:
                        counts["error"] += 1
                        self.stderr.write(f"    {csv_path.name}: ERROR — {e}")

            if not dry_run and participant is not None:
                self._update_summary_stats(participant)

        self.stdout.write(
            f"\nDone. ok={counts['ok']}  skip={counts['skip']}  error={counts['error']}"
        )

    # -------------------------------------------------------------------------

    def _get_study(self, study_name_or_id: str) -> Study:
        try:
            if len(study_name_or_id) == 24:
                return Study.objects.get(object_id=study_name_or_id)
            return Study.objects.get(name=study_name_or_id)
        except Study.DoesNotExist:
            raise CommandError(f"Study '{study_name_or_id}' not found.")

    def _infer_os_type(self, patient_dir: Path) -> str:
        streams = {p.name for p in patient_dir.iterdir() if p.is_dir()}
        if "ios_log" in streams:
            return IOS_API
        if "app_log" in streams or "texts" in streams or "calls" in streams:
            return ANDROID_API
        return ""

    def _update_summary_stats(self, participant: Participant):
        bounds = participant.chunk_registries.aggregate(earliest=Min("time_bin"), latest=Max("time_bin"))
        if bounds["earliest"] is None:
            return
        earliest_hour = int(bounds["earliest"].timestamp()) // CHUNK_TIMESLICE_QUANTUM
        latest_hour = int(bounds["latest"].timestamp()) // CHUNK_TIMESLICE_QUANTUM
        calculate_data_quantity_stats(participant, earliest_hour, latest_hour)
        self.stdout.write(f"  Updated summary statistics for {participant.patient_id}")

    def _get_or_create_participant(
        self, patient_id: str, study: Study, os_type: str
    ) -> tuple[Participant, bool]:
        try:
            return Participant.objects.get(patient_id=patient_id), False
        except Participant.DoesNotExist:
            pass
        participant = Participant(patient_id=patient_id, study=study, os_type=os_type)
        participant.reset_password()  # assigns a random password and saves
        return participant, True

    def _import_file(
        self,
        csv_path: Path,
        data_type: str,
        participant: Participant,
        study: Study,
        dry_run: bool,
    ) -> str:
        try:
            dt = self._parse_filename(csv_path.name)
        except Exception as e:
            return f"skip (unparseable filename: {e})"

        unix_ts = int(dt.timestamp())  # dt is already UTC-aware from fromisoformat
        hour = unix_ts // CHUNK_TIMESLICE_QUANTUM
        chunk_path = (
            f"{CHUNKS_FOLDER}/{study.object_id}/{participant.patient_id if not dry_run else '<patient_id>'}/"
            f"{data_type}/{dt.strftime('%Y-%m-%dT%H:%M:%S')}.csv"
        )

        if not dry_run and ChunkRegistry.objects.filter(chunk_path=chunk_path).exists():
            return "skip (already imported)"

        if dry_run:
            return f"dry-run -> {chunk_path}"

        csv_bytes = csv_path.read_bytes()
        sha1_bytes = hashlib.sha1(csv_bytes).digest()  # raw bytes for S3File
        md5_b64 = compute_chunk_hash(csv_bytes)        # base64 str for ChunkRegistry

        if data_type in CHUNKABLE_FILES:
            compressed = compress(csv_bytes)
            s3_upload_no_compression(
                chunk_path, compressed, participant, len(csv_bytes), sha1_bytes, raw_path=True
            )
            # time_bin is hours-since-epoch; register_chunked_data multiplies by CHUNK_TIMESLICE_QUANTUM
            ChunkRegistry.register_chunked_data(
                study_id=study.pk,
                participant_id=participant.pk,
                data_type=data_type,
                chunk_path=chunk_path,
                chunk_hash=md5_b64,
                time_bin=hour,
                survey_id=None,
                file_size=len(csv_bytes),
            )
        else:
            # unchunkable (survey_answers, audio recordings, etc.)
            s3_upload(chunk_path, csv_bytes, participant, raw_path=True)
            ChunkRegistry.register_unchunked_data(
                data_type=data_type,
                unix_timestamp=unix_ts,
                chunk_path=chunk_path,
                study_id=study.pk,
                participant_id=participant.pk,
                file_contents=csv_bytes,
                survey_id=None,
            )

        return f"ok -> {chunk_path}"

    @staticmethod
    def _parse_filename(filename: str) -> datetime:
        """Parse Beiwe export filenames like '2022-03-21 12_00_00+00_00.csv' to a UTC datetime."""
        stem = Path(filename).stem       # drop .csv
        iso = stem.replace("_", ":")    # underscores -> colons: '2022-03-21 12:00:00+00:00'
        iso = iso.replace(" ", "T", 1)  # space -> T:           '2022-03-21T12:00:00+00:00'
        return datetime.fromisoformat(iso)
