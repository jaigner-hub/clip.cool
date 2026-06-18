from django.urls import path

from . import views

urlpatterns = [
    path("clips/upload/", views.upload_page, name="clips_upload"),
    path("clips/upload/presign", views.presign, name="clips_presign"),
    path("clips/upload/finalize", views.finalize, name="clips_finalize"),
    path("clips/search/", views.search_page, name="clips_search"),
]
