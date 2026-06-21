from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    # Search IS the root (the discovery front door) — not a redirect.
    path("", views.search_page, name="clips_search"),

    # Canonical root URLs for a clip (short + shareable). One page per clip: humans get the full
    # page, chat/social unfurl off its OG/Twitter meta. .gif/.mp4 are direct-rendition links.
    path("<uuid:asset_id>", views.asset_detail, name="clips_asset"),
    path("<uuid:asset_id>.gif", views.public_clip_gif, name="clip_public_gif"),
    path("<uuid:asset_id>.mp4", views.public_clip_mp4, name="clip_public_mp4"),
    path("<uuid:asset_id>/download", views.clip_download, name="clip_download"),
    path("<uuid:asset_id>/download.gif", views.clip_download_gif, name="clip_download_gif"),

    # 301 from the old paths so links already shared keep working.
    path("c/<uuid:asset_id>/", RedirectView.as_view(pattern_name="clips_asset", permanent=True), name="clip_public"),
    path("c/<uuid:asset_id>.gif", RedirectView.as_view(pattern_name="clip_public_gif", permanent=True)),
    path("c/<uuid:asset_id>.mp4", RedirectView.as_view(pattern_name="clip_public_mp4", permanent=True)),
    path("clips/asset/<uuid:asset_id>/", RedirectView.as_view(pattern_name="clips_asset", permanent=True)),

    path("clips/", views.library, name="clips_library"),
    path("clips/record/", views.record_page, name="clips_record"),
    path("clips/upload/presign", views.presign, name="clips_presign"),
    path("clips/upload/finalize", views.finalize, name="clips_finalize"),
    path("clips/search/", RedirectView.as_view(pattern_name="clips_search", query_string=True, permanent=True)),
    path("clips/browse/", views.browse_page, name="clips_browse"),
    path("about/", views.about_page, name="clips_about"),
    path("clips/templates/", views.template_gallery, name="clips_templates"),
    path("clips/<uuid:asset_id>/remix/", views.remix_page, name="clips_remix"),
    path("clips/<uuid:asset_id>/remix", views.remix_create, name="clips_remix_create"),
    path("clips/asset/<uuid:asset_id>/status", views.asset_status, name="clips_asset_status"),
    path("clips/asset/<uuid:asset_id>/edit/", views.asset_edit, name="clips_edit"),
    path("clips/asset/<uuid:asset_id>/regenerate/", views.asset_regenerate, name="clips_regenerate"),
    path("clips/asset/<uuid:asset_id>/delete/", views.asset_delete, name="clips_delete"),
    path("clips/asset/<uuid:asset_id>/caption/", views.caption_builder, name="clips_caption"),
    path("clips/asset/<uuid:asset_id>/caption/save", views.caption_save, name="clips_caption_save"),
]
