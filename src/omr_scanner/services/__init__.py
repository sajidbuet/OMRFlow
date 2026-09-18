"""Application workflows.

Purpose:
    Coordinate domain models, the database, imaging, recognition and reporting
    into the operations a user actually performs ("create a project", "process a
    batch of scans", "calculate results").

Responsibilities:
    * Own multi-step operations and their failure handling.
    * Translate low-level failures into :mod:`omr_scanner.errors` exceptions the
      GUI can present.
    * Perform file system and database side effects; the domain layer never does.

What does NOT belong here:
    * Widgets, dialogs, Qt signals or any other presentation concern. A service
      must be callable from a test, a script or a future command line tool.
    * Image-processing algorithms (``imaging``) and value interpretation
      (``recognition``); services *call* those, they do not implement them.

Phase status:
    Phase 0 implements :mod:`omr_scanner.services.project_service` and
    :mod:`omr_scanner.services.template_service`; Phase 1 adds
    :mod:`omr_scanner.services.alignment_service`, the boundary that hands a
    template's geometry to the imaging layer. Phase 3 adds the scan workflow:
    :mod:`omr_scanner.services.recognition_service` (one sheet, end to end),
    :mod:`omr_scanner.services.batch_processor` (many of them, with progress and
    per-file error isolation), :mod:`omr_scanner.services.parallel_batch` (the
    worker-process pool that reads several sheets at once),
    :mod:`omr_scanner.services.filename_manager`
    (collision-free output names), :mod:`omr_scanner.services.scan_import`
    (what the user selected) and :mod:`omr_scanner.services.scan_export` (the
    results CSV). Conflict, attendance, scoring and Excel/PDF reporting services
    arrive with their own phases (see ``development/ROADMAP.md``).
"""

from omr_scanner.services.alignment_service import (
    alignment_config_from_template,
    load_scan_image,
    save_image,
)
from omr_scanner.services.batch_processor import (
    BatchOptions,
    BatchProgress,
    BatchReport,
    BatchStage,
    ProcessedScan,
    finalise_scan,
    plan_output_name,
    process_batch,
    process_scan,
)
from omr_scanner.services.batch_progress import (
    BatchProgressTracker,
    BatchState,
    JobStatus,
    ProgressSnapshot,
    format_count,
    format_duration,
    format_rate,
)
from omr_scanner.services.batch_store import (
    BatchIdentity,
    BatchRecorder,
    BatchStatus,
    BatchSummary,
    CompatibilityVerdict,
    ErrorCategory,
    ScanJobStatus,
    categorise_error,
    check_compatibility,
    completed_results,
    create_batch,
    delete_batch,
    failed_scans,
    finalise_batch,
    list_batches,
    load_summary,
    mark_cancelled,
    mark_queued,
    record_results,
    recover_interrupted,
    resumable_scans,
    scan_paths,
    set_batch_status,
)
from omr_scanner.services.calibration_service import (
    CalibrationFinding,
    CalibrationReport,
    CalibrationSampleReport,
    CalibrationStatus,
    aggregate_calibration,
    apply_calibration,
    evaluate_calibration,
    separation_label,
    write_calibration_report,
)
from omr_scanner.services.filename_manager import (
    FilenameAllocator,
    duplicate_suffix,
    sanitise_stem,
)
from omr_scanner.services.marker_detection_service import (
    ORIENTATION_DEBUG_IMAGE_NAME,
    DecodedImage,
    DetectedMarker,
    MarkerDetectionOutcome,
    MarkerSearchConfig,
    OrientationDetectionOutcome,
    OrientationSearchConfig,
    decode_image_file,
    detect_orientation_marker_in_region,
    detect_registration_markers,
)
from omr_scanner.services.project_service import (
    ProjectDatabase,
    ProjectSession,
    create_project,
    is_project_directory,
    open_project,
    read_project_metadata,
)
from omr_scanner.services.recognition_diagnostics import (
    render_overlay,
    write_diagnostics,
)
from omr_scanner.services.recognition_models import (
    ENGINE_NAME,
    ENGINE_VERSION,
    RESULT_SCHEMA_VERSION,
    AnswerView,
    BubbleView,
    CharacterView,
    FieldView,
    MarkerView,
    RecognitionOutcome,
    RegistrationStatus,
    ScanQuality,
    ScanResult,
    StageTimings,
    StatusCode,
    ZoneView,
)
from omr_scanner.services.recognition_service import (
    CalibrationSession,
    RecognitionEngine,
    recognise_scan,
)
from omr_scanner.services.recognition_settings import (
    DiagnosticsOptions,
    RecognitionOptions,
)
from omr_scanner.services.scan_export import export_scan_results, render_scan_results
from omr_scanner.services.scan_import import (
    SUPPORTED_SCAN_SUFFIXES,
    collect_scan_files,
    is_supported_scan,
    natural_sort_key,
)
from omr_scanner.services.template_service import (
    list_templates,
    load_template,
    save_template,
)

__all__ = [
    "ENGINE_NAME",
    "ENGINE_VERSION",
    "ORIENTATION_DEBUG_IMAGE_NAME",
    "RESULT_SCHEMA_VERSION",
    "SUPPORTED_SCAN_SUFFIXES",
    "AnswerView",
    "BatchIdentity",
    "BatchOptions",
    "BatchProgress",
    "BatchProgressTracker",
    "BatchRecorder",
    "BatchReport",
    "BatchStage",
    "BatchState",
    "BatchStatus",
    "BatchSummary",
    "BubbleView",
    "CalibrationFinding",
    "CalibrationReport",
    "CalibrationSampleReport",
    "CalibrationSession",
    "CalibrationStatus",
    "CharacterView",
    "CompatibilityVerdict",
    "DecodedImage",
    "DetectedMarker",
    "DiagnosticsOptions",
    "ErrorCategory",
    "FieldView",
    "FilenameAllocator",
    "JobStatus",
    "MarkerDetectionOutcome",
    "MarkerSearchConfig",
    "MarkerView",
    "OrientationDetectionOutcome",
    "OrientationSearchConfig",
    "ProcessedScan",
    "ProgressSnapshot",
    "ProjectDatabase",
    "ProjectSession",
    "RecognitionEngine",
    "RecognitionOptions",
    "RecognitionOutcome",
    "RegistrationStatus",
    "ScanJobStatus",
    "ScanQuality",
    "ScanResult",
    "StageTimings",
    "StatusCode",
    "ZoneView",
    "aggregate_calibration",
    "alignment_config_from_template",
    "apply_calibration",
    "categorise_error",
    "check_compatibility",
    "collect_scan_files",
    "completed_results",
    "create_batch",
    "create_project",
    "decode_image_file",
    "delete_batch",
    "detect_orientation_marker_in_region",
    "detect_registration_markers",
    "duplicate_suffix",
    "evaluate_calibration",
    "export_scan_results",
    "failed_scans",
    "finalise_batch",
    "finalise_scan",
    "format_count",
    "format_duration",
    "format_rate",
    "is_project_directory",
    "is_supported_scan",
    "list_batches",
    "list_templates",
    "load_scan_image",
    "load_summary",
    "load_template",
    "mark_cancelled",
    "mark_queued",
    "natural_sort_key",
    "open_project",
    "plan_output_name",
    "process_batch",
    "process_scan",
    "read_project_metadata",
    "recognise_scan",
    "record_results",
    "recover_interrupted",
    "render_overlay",
    "render_scan_results",
    "resumable_scans",
    "sanitise_stem",
    "save_image",
    "save_template",
    "scan_paths",
    "separation_label",
    "set_batch_status",
    "write_calibration_report",
    "write_diagnostics",
]
