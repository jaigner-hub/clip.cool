from django.urls import path

from . import views

urlpatterns = [
    path("c/<uuid:asset_id>/", views.public_clip, name="clip_public"),   # short public share URL
    path("c/<uuid:asset_id>.mp4", views.public_clip_mp4, name="clip_public_mp4"),  # direct video (GIF-style embeds)
    path("clips/", views.library, name="clips_library"),
    path("clips/upload/", views.upload_page, name="clips_upload"),
    path("clips/upload/presign", views.presign, name="clips_presign"),
    path("clips/upload/finalize", views.finalize, name="clips_finalize"),
    path("clips/search/", views.search_page, name="clips_search"),
    path("clips/create/", views.create_gallery, name="clips_create"),
    path("clips/create/<uuid:template_id>/", views.builder, name="clips_builder"),
    path("clips/template/<uuid:template_id>/raw", views.template_image, name="clips_template_image"),
    path("clips/asset/<uuid:asset_id>/", views.asset_detail, name="clips_asset"),
    path("clips/asset/<uuid:asset_id>/edit/", views.asset_edit, name="clips_edit"),
    path("clips/asset/<uuid:asset_id>/regenerate/", views.asset_regenerate, name="clips_regenerate"),
    path("clips/asset/<uuid:asset_id>/caption/", views.caption_builder, name="clips_caption"),
    path("clips/asset/<uuid:asset_id>/caption/save", views.caption_save, name="clips_caption_save"),
]
