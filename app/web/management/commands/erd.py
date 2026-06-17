"""Generate the complete data-model ER diagram (Mermaid) from the Django models and splice it
into docs/architecture.md, in place, between marker comments.

The point: the architecture doc's full schema is *never* hand-maintained or copy-pasted. This
command introspects every first-party model (via each app's ``_meta``) and rewrites only the
text between the BEGIN/END markers — so a model change is one regeneration away from a correct
diagram, and `.githooks/pre-commit` runs it automatically when any models.py changes.

    python manage.py erd            # rewrite the block in docs/architecture.md
    python manage.py erd --check    # exit 1 if the block is stale (CI / pre-commit guard)
    python manage.py erd --stdout   # print the generated block, write nothing

Determinism: the output has no timestamps and is fully sorted, so --check is stable and the
pre-commit hook only restages the doc when the schema genuinely changed.
"""
import re
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import models

BEGIN = "<!-- BEGIN GENERATED ERD — produced by `manage.py erd`; do not edit between the markers -->"
END = "<!-- END GENERATED ERD -->"

# Django internal field type -> short ER attribute type.
TYPE_MAP = {
    "AutoField": "int", "BigAutoField": "bigint", "SmallAutoField": "int",
    "CharField": "string", "SlugField": "string", "EmailField": "string", "URLField": "string",
    "TextField": "text", "BooleanField": "boolean",
    "DateTimeField": "datetime", "DateField": "date", "TimeField": "time",
    "IntegerField": "int", "BigIntegerField": "bigint", "PositiveIntegerField": "int",
    "PositiveSmallIntegerField": "int", "SmallIntegerField": "int",
    "FloatField": "float", "DecimalField": "decimal",
    "JSONField": "json", "UUIDField": "uuid",
}


def entity_name(model):
    """CamelCase model name -> UPPER_SNAKE entity name (Organization -> ORGANIZATION,
    OrganizationMembership -> ORGANIZATION_MEMBERSHIP, matching the curated diagram's style)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", model.__name__).upper()


def attr_type(field):
    internal = field.get_internal_type()
    return TYPE_MAP.get(internal, internal.replace("Field", "").lower() or "string")


def is_first_party(app_config):
    """A model app we own — its code lives in the repo, not in an installed package."""
    path = str(app_config.path)
    return "/site-packages/" not in path and "/dist-packages/" not in path


def single_field_unique(model):
    """Field names made unique on their own (UniqueConstraint/unique_together of length 1) —
    so a FK with such a constraint reads as one-to-one (e.g. one_org_per_user)."""
    names = set()
    for c in model._meta.constraints:
        if isinstance(c, models.UniqueConstraint) and len(c.fields) == 1 and not c.condition:
            names.add(c.fields[0])
    for ut in model._meta.unique_together:
        if len(ut) == 1:
            names.add(ut[0])
    return names


class Command(BaseCommand):
    help = "Regenerate the data-model ER diagram in docs/architecture.md from the Django models."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check", action="store_true",
            help="Exit 1 if the diagram in the doc is stale; write nothing.",
        )
        parser.add_argument(
            "--print", dest="show", action="store_true",
            help="Print the generated Mermaid block and write nothing.",
        )
        parser.add_argument(
            "--target", default=None,
            help="Markdown file to splice into (default: <repo>/docs/architecture.md).",
        )

    def handle(self, *args, **opts):
        user_model = get_user_model()
        first_party = {
            m for ac in apps.get_app_configs() if is_first_party(ac) for m in ac.get_models()
        }
        # The User model lives in an installed package, but first-party FKs point at it — render
        # it with a representative (not exhaustive) attribute set so it isn't a bare node.
        primary = sorted(first_party, key=lambda m: (m._meta.app_label, m.__name__))

        relationships = set()
        entities = {}  # entity_name -> list[str] attribute lines (insertion order preserved)

        for model in primary:
            uniq = single_field_unique(model)
            attrs = []
            for f in model._meta.local_fields:
                if f.is_relation:
                    # FK / O2O -> an edge, not an attribute.
                    target = f.related_model
                    if target is None:
                        continue
                    one_to_one = f.one_to_one or f.name in uniq
                    left = "||" if not f.null else "|o"            # required vs optional parent
                    right = "o|" if one_to_one else "o{"           # one vs many children
                    relationships.add(
                        f"{entity_name(target)} {left}--{right} {entity_name(model)} : {f.name}"
                    )
                    continue
                key = "PK" if f.primary_key else ("UK" if (f.unique or f.name in uniq) else "")
                attrs.append(_attr_line(attr_type(f), f.name, key))
            for f in model._meta.local_many_to_many:
                target = f.related_model
                if target is not None:
                    relationships.add(
                        f"{entity_name(model)} }}o--o{{ {entity_name(target)} : {f.name}"
                    )
            entities[entity_name(model)] = attrs

        # Add a representative USER entity if any first-party model references it.
        user_ent = entity_name(user_model)
        if any(user_ent in r for r in relationships) and user_ent not in entities:
            entities[user_ent] = [
                _attr_line("string", "email", "UK"),
                _attr_line("boolean", "is_staff"),
                _attr_line("boolean", "is_superuser"),
            ]

        mermaid = self._render(relationships, entities)

        if opts["show"]:
            self.stdout.write(mermaid)
            return

        target = Path(opts["target"]) if opts["target"] else settings.BASE_DIR.parent / "docs" / "architecture.md"
        if not target.exists():
            raise CommandError(f"target not found: {target}")
        content = target.read_text()
        if BEGIN not in content or END not in content:
            raise CommandError(
                f"markers not found in {target}. Add a block:\n\n{BEGIN}\n{END}\n"
            )
        pre = content[: content.index(BEGIN)]
        post = content[content.index(END) + len(END):]
        new = f"{pre}{BEGIN}\n\n{mermaid}\n\n{END}{post}"

        n_models, n_rels = len(entities), len(relationships)
        if new == content:
            self.stdout.write(self.style.SUCCESS(f"ERD up to date ({n_models} entities, {n_rels} relationships)."))
            return
        if opts["check"]:
            raise CommandError(
                f"ERD in {target.name} is stale — run `python manage.py erd` and commit the result."
            )
        target.write_text(new)
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {target} ({n_models} entities, {n_rels} relationships)."
        ))

    def _render(self, relationships, entities):
        lines = ["```mermaid", "erDiagram"]
        for r in sorted(relationships):
            lines.append(f"  {r}")
        if relationships and entities:
            lines.append("")
        for name in sorted(entities):
            lines.append(f"  {name} {{")
            for a in entities[name]:
                lines.append(f"    {a}")
            lines.append("  }")
        lines.append("```")
        return "\n".join(lines)


def _attr_line(type_, name, key=""):
    # type · name · (PK/UK/FK) — no comment column on purpose: enum choices belong in enums.py,
    # not the ERD, and stuffing them here forced the tables absurdly wide.
    return f"{type_} {name} {key}".rstrip()
