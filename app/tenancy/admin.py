"""Django admin = the staff onboarding surface (ADR 0009): create an Organization,
add its members, create its projects. Admin needs is_staff (mapped from Keycloak
roles) and sits behind Cloudflare Access.
"""
from django.contrib import admin

from .models import Organization, OrganizationMembership, Project, ServiceAccount


class MembershipInline(admin.TabularInline):
    model = OrganizationMembership
    extra = 1
    autocomplete_fields = ["user"]


class ProjectInline(admin.TabularInline):
    model = Project
    extra = 0
    prepopulated_fields = {"slug": ("name",)}
    fields = ["name", "slug", "is_active"]


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "is_active", "created_at"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}
    inlines = [MembershipInline, ProjectInline]


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "organization", "role", "created_at"]
    list_filter = ["role", "organization"]
    search_fields = ["user__email", "user__username", "organization__name"]
    autocomplete_fields = ["user", "organization"]


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ["name", "organization", "slug", "is_active", "created_at"]
    list_filter = ["organization", "is_active"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}


@admin.register(ServiceAccount)
class ServiceAccountAdmin(admin.ModelAdmin):
    list_display = ["client_id", "organization", "label", "is_active", "created_at"]
    list_filter = ["organization", "is_active"]
    search_fields = ["client_id", "label", "organization__name"]
    autocomplete_fields = ["organization"]
