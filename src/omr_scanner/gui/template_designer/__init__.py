"""The interactive OMR template designer (Phase 2).

Purpose:
    Let a user author a ``.omrt`` template visually: load a reference sheet
    image, detect and adjust its registration markers, and draw the regions
    (student ID, question set, question blocks, custom bubble groups) that
    describe every bubble the sheet contains.

Responsibilities:
    * Present the reference image and overlay editable geometry on a
      zoomable/pannable canvas (:mod:`canvas`, :mod:`items`).
    * Convert between display, image-pixel and normalised template coordinates
      in one place (:mod:`coordinates`).
    * Hold the in-progress :class:`~omr_scanner.domain.template.OmrTemplate`
      and its undo/redo history (:mod:`state`, :mod:`history`).
    * Offer region-generation dialogs backed by
      :mod:`omr_scanner.domain.template_authoring` (:mod:`dialogs`).
    * Assemble the whole thing into the workflow page shown in the main window
      (:mod:`page`).

What does NOT belong here:
    * Any ``cv2``/``numpy`` import - decoding images and running marker
      detection is :mod:`omr_scanner.services.marker_detection_service`. This
      package receives only plain Python values and Qt types from it.
    * Template schema or validation rules - those live in
      :mod:`omr_scanner.domain.template` and
      :mod:`omr_scanner.domain.template_authoring`; this package calls them.

See ``docs/template_designer.md`` for the user-facing description and
``docs/phase_02_plan.md`` for the design decisions behind this layout.
"""
