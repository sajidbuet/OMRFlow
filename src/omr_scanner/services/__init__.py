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
    per-file error isolation), :mod:`omr_scanner.services.filename_manager`
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
    plan_output_name,
    process_batch,
    process_scan,
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
    ProjectSession,
    create_project,
    is_project_directory,
    open_project,
    read_project_metadata,
)
from omr_scanner.services.recognition_service import (
    AnswerView,
    BubbleView,
    CharacterView,
    FieldView,
    MarkerView,
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
    ZoneView,
    recognise_scan,
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
    "ORIENTATION_DEBUG_IMAGE_NAME",
    "SUPPORTED_SCAN_SUFFIXES",
    "AnswerView",
    "BatchOptions",
    "BatchProgress",
    "BatchReport",
    "BatchStage",
    "BubbleView",
    "CharacterView",
    "DecodedImage",
    "DetectedMarker",
    "FieldView",
    "FilenameAllocator",
    "MarkerDetectionOutcome",
    "MarkerSearchConfig",
    "MarkerView",
    "OrientationDetectionOutcome",
    "OrientationSearchConfig",
    "ProcessedScan",
    "ProjectSession",
    "RecognitionOutcome",
    "RegistrationStatus",
    "ScanResult",
    "ZoneView",
    "alignment_config_from_template",
    "collect_scan_files",
    "create_project",
    "decode_image_file",
    "detect_orientation_marker_in_region",
    "detect_registration_markers",
    "duplicate_suffix",
    "export_scan_results",
    "is_project_directory",
    "is_supported_scan",
    "list_templates",
    "load_scan_image",
    "load_template",
    "natural_sort_key",
    "open_project",
    "plan_output_name",
    "process_batch",
    "process_scan",
    "read_project_metadata",
    "recognise_scan",
    "render_scan_results",
    "sanitise_stem",
    "save_image",
    "save_template",
]
