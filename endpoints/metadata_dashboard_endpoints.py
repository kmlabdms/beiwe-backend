from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from authentication.admin_authentication import (authenticate_researcher_study_access,
    ResearcherRequest)
from config.settings import METADATA_INDEX_ENABLED
from database.study_models import Study
from libs import metadata_index_reader


@require_GET
@authenticate_researcher_study_access
def metadata_dashboard_page(request: ResearcherRequest, study_id: int):
    """Per-study upload-activity monitor over the DynamoDB Upload Metadata Index.

    Read-only; per-study scoping is enforced by @authenticate_researcher_study_access
    (the researcher must have access to study_id, else 403/404 before the body runs).
    Degrades to three distinct, non-500 states: not_configured (opt-in feature off),
    read_error (DynamoDB/STS unreachable -- shown loudly, never alongside zero data),
    and ok (which itself renders a "no data yet" notice when the index is empty).
    """
    study = get_object_or_404(Study, pk=study_id)
    context = dict(
        study=study,
        study_id=study_id,
        page_location="metadata_dashboard",
        metadata_index_enabled=METADATA_INDEX_ENABLED,
    )

    # The decorator guarantees study exists; object_id is the DynamoDB STUDY#<id> key.
    object_id = Study.value_get("object_id", pk=study_id)
    try:
        context.update(state="ok", summary=metadata_index_reader.study_summary(object_id))
    except metadata_index_reader.MetadataIndexNotConfigured:
        context.update(state="not_configured")
    except (metadata_index_reader.MetadataIndexReadError,
            metadata_index_reader.MetadataIndexInvalidStudy):
        # Both are operator-facing problems (bad config / unreachable index); show the
        # loud read-error state rather than silently looking "disabled" or "empty".
        context.update(state="read_error")

    return render(request, "metadata_dashboard/metadata_dashboard.html", context=context)
