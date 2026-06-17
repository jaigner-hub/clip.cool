from django.urls import path

from . import views

urlpatterns = [
    path("recommendations/playground/", views.playground, name="rec_playground"),
    path("recommendations/playground/stream", views.playground_stream, name="rec_playground_stream"),
    path("recommendations/playground/prompt", views.playground_save_prompt, name="rec_playground_save_prompt"),
    path(
        "recommendations/playground/interaction/<int:rec_id>",
        views.playground_interaction, name="rec_playground_interaction",
    ),
]
