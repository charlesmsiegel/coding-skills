#!/usr/bin/env python3
"""What Django removed, when, and what to write instead.

Django deprecates on a published schedule and removes two feature releases
later. That makes a dated idiom something more specific than unfashionable: it
is a crash with a known version number. So the interesting question is never
"is this old?" but "does this survive the version we are moving to?" — and that
question has a table-shaped answer.

Each row is one construct:

    Change(name="index_together", deprecated_in=(4, 2), removed_in=(5, 1),
           match={"kind": "meta_option", "name": "index_together"},
           replacement="Meta.indexes = [models.Index(fields=[...])]")

``match["kind"]`` is the shape the construct takes in source, and there are only
seven of them. That is what lets one generic matcher in find_version_issues.py
serve the whole table, and why adding Django 6.2 or 7.0 later is a data edit
rather than a code edit.

The table is a snapshot as of Django 6.1 (August 2026) — see LATEST_KNOWN. It
is deliberately not exhaustive: it carries what appears in *application* code
and skips what only appears in database-backend subclasses, because a table
nobody trusts is worse than a shorter one that is right.
"""

from dataclasses import dataclass, field

# The newest release this table knows about. When a caller targets something
# past this, find_version_issues says so rather than implying it checked.
LATEST_KNOWN = (6, 1)

# Releases and their support status, as of August 2026.
#   security_until=None means the release no longer receives security fixes.
SUPPORTED = {
    (4, 0): {"lts": False, "released": "2021-12", "security_until": None},
    (4, 1): {"lts": False, "released": "2022-08", "security_until": None},
    (4, 2): {"lts": True, "released": "2023-04", "security_until": None},   # ended 2026-04
    (5, 0): {"lts": False, "released": "2023-12", "security_until": None},
    (5, 1): {"lts": False, "released": "2024-08", "security_until": None},
    (5, 2): {"lts": True, "released": "2025-04", "security_until": "2028-04"},
    (6, 0): {"lts": False, "released": "2025-12", "security_until": "2027-04"},
    (6, 1): {"lts": False, "released": "2026-08", "security_until": "2027-12"},
}

# The versions worth landing on: the current release, and the LTS for anyone who
# would rather move once every two years than once every eight months.
CURRENT_RELEASE = (6, 1)
CURRENT_LTS = (5, 2)

MATCH_KINDS = frozenset({
    "setting",           # a module-level assignment in a settings module
    "import",            # `from <module> import <name>`
    "call",              # a call, matched on the last component of its target
    "kwarg",             # a keyword argument on a named call
    "meta_option",       # an assignment inside an inner `class Meta`
    "attribute",         # an attribute access, matched by name
    "template_filter",   # a filter used in a template
})


@dataclass(frozen=True)
class Change:
    """One Django construct that is going away, or already has."""

    name: str
    match: dict
    replacement: str
    deprecated_in: tuple = None
    removed_in: tuple = None
    note: str = ""
    # Where the construct can appear. Narrowing this is what keeps a settings
    # rule from firing on a local variable in application code.
    scope: str = "any"          # "any" | "settings" | "models" | "templates"
    aliases: tuple = field(default_factory=tuple)

    def __post_init__(self):
        if self.match.get("kind") not in MATCH_KINDS:
            raise ValueError("unknown match kind in " + self.name)
        if self.deprecated_in is None and self.removed_in is None:
            raise ValueError("change " + self.name + " has no version at all")


def parse_version(text):
    """The first `major.minor` in a requirement string, or None.

    Handles what actually appears in manifests: "5.2", "Django>=4.2,<5.0",
    "django~=6.1.0", "Django == 5.2.3". Takes the *lower* bound, because that is
    the version the project promises to run on — an upper bound of <5.0 says
    nothing about whether the code is 4.0 or 4.2 shaped.
    """
    if not text:
        return None
    digits = []
    current = ""
    for char in str(text):
        if char.isdigit() or char == ".":
            current += char
        else:
            if current:
                digits.append(current)
            current = ""
    if current:
        digits.append(current)

    for token in digits:
        parts = [p for p in token.split(".") if p != ""]
        if not parts or not parts[0].isdigit():
            continue
        major = int(parts[0])
        # A bare "4" is a major-version pin; treat it as its first release.
        minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if 1 <= major <= 20:            # a Django major, not a stray year or hash
            return (major, minor)
    return None


def is_end_of_life(version):
    """Whether this release still receives security fixes.

    Unknown versions are not called end-of-life: an unreleased future version
    and an ancient one are both absent from the table, and guessing wrong in the
    alarming direction is how a finding stops being believed.
    """
    if version is None:
        return False
    info = SUPPORTED.get(tuple(version[:2]))
    if info is None:
        return version < min(SUPPORTED)
    return info["security_until"] is None


def describe(version):
    """'5.2 LTS' / '6.1' / 'unknown'."""
    if version is None:
        return "unknown"
    text = str(version[0]) + "." + str(version[1])
    info = SUPPORTED.get(tuple(version[:2]))
    if info and info["lts"]:
        text += " LTS"
    return text


# --------------------------------------------------------------------------- #
# The table. Ordered by the release that removed (or will remove) each item.
# --------------------------------------------------------------------------- #

CHANGES = [
    # ---- removed in 4.0 --------------------------------------------------- #
    Change(
        name="url",
        match={"kind": "call", "name": "url"},
        replacement="path('orders/<int:pk>/', view, name='...'), or re_path() where a real regex is needed",
        deprecated_in=(3, 1), removed_in=(4, 0),
        note="path() converts <int:pk> to an actual int; the regex gave you a string.",
    ),
    Change(
        name="ugettext",
        match={"kind": "import", "module": "django.utils.translation", "name": "ugettext"},
        replacement="gettext",
        deprecated_in=(3, 0), removed_in=(4, 0),
        aliases=("ugettext_lazy", "ugettext_noop", "ungettext", "ungettext_lazy"),
    ),
    Change(
        name="force_text",
        match={"kind": "import", "module": "django.utils.encoding", "name": "force_text"},
        replacement="force_str",
        deprecated_in=(3, 0), removed_in=(4, 0),
        aliases=("smart_text",),
    ),

    # ---- removed in 5.0 (deprecated in 4.0 / 4.1) ------------------------- #
    Change(
        name="USE_L10N",
        match={"kind": "setting", "name": "USE_L10N"},
        replacement="delete the setting — localization is always on",
        deprecated_in=(4, 0), removed_in=(5, 0), scope="settings",
    ),
    Change(
        name="USE_DEPRECATED_PYTZ",
        match={"kind": "setting", "name": "USE_DEPRECATED_PYTZ"},
        replacement="delete it and move to zoneinfo",
        deprecated_in=(4, 0), removed_in=(5, 0), scope="settings",
        note="pytz support went with it. Naive-datetime code that leaned on pytz.localize() needs rewriting.",
    ),
    Change(
        name="CSRF_COOKIE_MASKED",
        match={"kind": "setting", "name": "CSRF_COOKIE_MASKED"},
        replacement="delete it",
        deprecated_in=(4, 1), removed_in=(5, 0), scope="settings",
    ),
    Change(
        name="timezone.utc",
        match={"kind": "import", "module": "django.utils.timezone", "name": "utc"},
        replacement="datetime.timezone.utc",
        deprecated_in=(4, 1), removed_in=(5, 0),
    ),
    Change(
        name="baseconv",
        match={"kind": "import", "module": "django.utils", "name": "baseconv"},
        replacement="a third-party base-conversion library, or write the four lines",
        deprecated_in=(4, 0), removed_in=(5, 0),
    ),
    Change(
        name="datetime_safe",
        match={"kind": "import", "module": "django.utils", "name": "datetime_safe"},
        replacement="datetime — Python 3.11 no longer needs the shim",
        deprecated_in=(4, 0), removed_in=(5, 0),
    ),
    Change(
        name="PickleSerializer",
        match={"kind": "import", "module": "django.contrib.sessions.serializers", "name": "PickleSerializer"},
        replacement="JSONSerializer",
        deprecated_in=(4, 1), removed_in=(5, 0),
        note="Pickled sessions execute arbitrary code if the signing key ever leaks. This one is a security fix, not tidying.",
    ),
    Change(
        name="CryptPasswordHasher",
        match={"kind": "attribute", "name": "CryptPasswordHasher"},
        replacement="the default PBKDF2 hasher, or Argon2",
        deprecated_in=(4, 1), removed_in=(5, 0),
    ),
    Change(
        name="is_dst",
        match={"kind": "kwarg", "call": "make_aware", "name": "is_dst"},
        replacement="drop the argument; zoneinfo resolves ambiguous times by fold=",
        deprecated_in=(4, 0), removed_in=(5, 0),
    ),

    # ---- removed in 5.1 (deprecated in 4.2) -------------------------------- #
    Change(
        name="index_together",
        match={"kind": "meta_option", "name": "index_together"},
        replacement="Meta.indexes = [models.Index(fields=[...])]",
        deprecated_in=(4, 2), removed_in=(5, 1), scope="models",
        note="makemigrations emits a RenameIndex operation for indexes that already exist.",
    ),
    Change(
        name="DEFAULT_FILE_STORAGE",
        match={"kind": "setting", "name": "DEFAULT_FILE_STORAGE"},
        replacement="STORAGES = {'default': {'BACKEND': '...'}, 'staticfiles': {...}}",
        deprecated_in=(4, 2), removed_in=(5, 1), scope="settings",
    ),
    Change(
        name="STATICFILES_STORAGE",
        match={"kind": "setting", "name": "STATICFILES_STORAGE"},
        replacement="STORAGES['staticfiles']['BACKEND']",
        deprecated_in=(4, 2), removed_in=(5, 1), scope="settings",
    ),
    Change(
        name="get_storage_class",
        match={"kind": "import", "module": "django.core.files.storage", "name": "get_storage_class"},
        replacement="django.core.files.storage.storages['default']",
        deprecated_in=(4, 2), removed_in=(5, 1),
    ),
    Change(
        name="length_is",
        match={"kind": "template_filter", "name": "length_is"},
        replacement="{% if value|length == 4 %}",
        deprecated_in=(4, 2), removed_in=(5, 1), scope="templates",
    ),
    Change(
        name="make_random_password",
        match={"kind": "call", "name": "make_random_password"},
        replacement="secrets.token_urlsafe(), or get_random_string()",
        deprecated_in=(4, 2), removed_in=(5, 1),
    ),
    Change(
        name="CICharField",
        match={"kind": "call", "name": "CICharField"},
        replacement="CharField(db_collation='case_insensitive_collation')",
        deprecated_in=(4, 2), removed_in=(5, 1),
        aliases=("CIEmailField", "CITextField"),
        note="Still importable inside historical migrations; only new model code has to change.",
    ),
    Change(
        name="SHA1PasswordHasher",
        match={"kind": "attribute", "name": "SHA1PasswordHasher"},
        replacement="PBKDF2PasswordHasher, or Argon2PasswordHasher",
        deprecated_in=(4, 2), removed_in=(5, 1),
        aliases=("UnsaltedSHA1PasswordHasher", "UnsaltedMD5PasswordHasher"),
        note="Removing a hasher from PASSWORD_HASHERS locks out every user still stored under it. "
             "Force a reset, or keep the hasher until they have all logged in once.",
    ),
    Change(
        name="assertFormsetError",
        match={"kind": "call", "name": "assertFormsetError"},
        replacement="assertFormSetError (capital S)",
        deprecated_in=(4, 2), removed_in=(5, 1),
    ),
    Change(
        name="assertQuerysetEqual",
        match={"kind": "call", "name": "assertQuerysetEqual"},
        replacement="assertQuerySetEqual (capital S)",
        deprecated_in=(4, 2), removed_in=(5, 1),
    ),

    # ---- removed in 6.0 (deprecated in 5.0 / 5.1) -------------------------- #
    Change(
        name="ChoicesMeta",
        match={"kind": "attribute", "name": "ChoicesMeta"},
        replacement="django.db.models.enums.ChoicesType",
        deprecated_in=(5, 0), removed_in=(6, 0),
    ),
    Change(
        name="FORMS_URLFIELD_ASSUME_HTTPS",
        match={"kind": "setting", "name": "FORMS_URLFIELD_ASSUME_HTTPS"},
        replacement="delete it — forms.URLField assumes https from 6.0",
        deprecated_in=(5, 0), removed_in=(6, 0), scope="settings",
    ),
    Change(
        name="DjangoDivFormRenderer",
        match={"kind": "attribute", "name": "DjangoDivFormRenderer"},
        replacement="DjangoTemplates — div rendering is the default now",
        deprecated_in=(5, 0), removed_in=(6, 0),
        aliases=("Jinja2DivFormRenderer",),
    ),
    Change(
        name="get_prefetch_queryset",
        match={"kind": "call", "name": "get_prefetch_queryset"},
        replacement="get_prefetch_querysets()",
        deprecated_in=(5, 0), removed_in=(6, 0),
    ),
    Change(
        name="CheckConstraint.check",
        match={"kind": "kwarg", "call": "CheckConstraint", "name": "check"},
        replacement="CheckConstraint(condition=...)",
        deprecated_in=(5, 1), removed_in=(6, 0), scope="models",
    ),
    Change(
        name="itercompat.is_iterable",
        match={"kind": "import", "module": "django.utils.itercompat", "name": "is_iterable"},
        replacement="isinstance(x, collections.abc.Iterable)",
        deprecated_in=(5, 1), removed_in=(6, 0),
    ),
    Change(
        name="OS_OPEN_FLAGS",
        match={"kind": "attribute", "name": "OS_OPEN_FLAGS"},
        replacement="FileSystemStorage(allow_overwrite=True)",
        deprecated_in=(5, 1), removed_in=(6, 0),
    ),
    Change(
        name="log_deletion",
        match={"kind": "call", "name": "log_deletion"},
        replacement="ModelAdmin.log_deletions()",
        deprecated_in=(5, 1), removed_in=(6, 0),
    ),

    # ---- removed in 6.1 (deprecated in 5.2) -------------------------------- #
    Change(
        name="staticfiles.find(all=...)",
        match={"kind": "kwarg", "call": "find", "name": "all"},
        replacement="find(..., find_all=True)",
        deprecated_in=(5, 2), removed_in=(6, 1),
    ),
    Change(
        name="postgres_aggregate_ordering",
        match={"kind": "kwarg", "call": "StringAgg", "name": "ordering"},
        replacement="order_by=",
        deprecated_in=(5, 2), removed_in=(6, 1),
        note="Same change on ArrayAgg and JSONBAgg.",
    ),
    Change(
        name="ArrayAgg.ordering",
        match={"kind": "kwarg", "call": "ArrayAgg", "name": "ordering"},
        replacement="order_by=",
        deprecated_in=(5, 2), removed_in=(6, 1),
    ),

    # ---- deprecated now, removed in 7.0 ------------------------------------ #
    Change(
        name="URLIZE_ASSUME_HTTPS",
        match={"kind": "setting", "name": "URLIZE_ASSUME_HTTPS"},
        replacement="delete it once you are on 7.0 — urlize assumes https there",
        deprecated_in=(6, 0), removed_in=(7, 0), scope="settings",
    ),
    Change(
        name="EMAIL_BACKEND",
        match={"kind": "setting", "name": "EMAIL_BACKEND"},
        replacement="MAILERS = {'default': {'BACKEND': '...'}}",
        deprecated_in=(6, 1), removed_in=(7, 0), scope="settings",
        note="The whole EMAIL_* family moves into MAILERS, the way DATABASES and STORAGES already work.",
    ),
    Change(
        name="EMAIL_HOST",
        match={"kind": "setting", "name": "EMAIL_HOST"},
        replacement="MAILERS['default']['OPTIONS']",
        deprecated_in=(6, 1), removed_in=(7, 0), scope="settings",
        aliases=("EMAIL_PORT", "EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD",
                 "EMAIL_USE_TLS", "EMAIL_USE_SSL", "EMAIL_TIMEOUT"),
    ),
    Change(
        name="mail.get_connection",
        match={"kind": "call", "name": "get_connection"},
        replacement="mail.mailers['default']",
        deprecated_in=(6, 1), removed_in=(7, 0),
    ),
    Change(
        name="select_related_no_args",
        match={"kind": "call", "name": "select_related", "no_args": True},
        replacement="name the relations: select_related('customer', 'customer__region')",
        deprecated_in=(6, 1), removed_in=(7, 0),
        note="Argument-less select_related joins every non-null FK, which is rarely what anyone wanted.",
    ),
    Change(
        name="values_list_flat_no_field",
        match={"kind": "kwarg", "call": "values_list", "name": "flat", "requires_no_positional": True},
        replacement="values_list('field', flat=True)",
        deprecated_in=(6, 1), removed_in=(7, 0),
    ),
    Change(
        name="transaction.savepoint",
        match={"kind": "call", "name": "savepoint"},
        replacement="transaction.savepoint_create()",
        deprecated_in=(6, 1), removed_in=(7, 0),
    ),
    Change(
        name="BLANK_CHOICE_DASH",
        match={"kind": "attribute", "name": "BLANK_CHOICE_DASH"},
        replacement="drop it; Django renders the blank choice itself",
        deprecated_in=(6, 1), removed_in=(7, 0),
    ),
    Change(
        name="ADMINS_as_tuples",
        match={"kind": "setting", "name": "ADMINS"},
        replacement="a list of plain email address strings",
        deprecated_in=(6, 0), removed_in=(7, 0), scope="settings",
        aliases=("MANAGERS",),
        note="Only the (name, address) tuple form is going; a list of strings is already correct.",
    ),
]


def changes_between(current, target):
    """Every change that matters when moving from ``current`` to ``target``.

    A change matters when it is removed at or before the target (the project
    breaks), or is deprecated at or before the target (it works, on a clock).
    """
    relevant = []
    for change in CHANGES:
        removed = change.removed_in
        deprecated = change.deprecated_in
        if removed is not None and target is not None and removed <= target:
            relevant.append(change)
        elif deprecated is not None and target is not None and deprecated <= target:
            relevant.append(change)
        elif target is None:
            relevant.append(change)
    return relevant


def all_names(change):
    """The construct's own name plus every alias, for matching."""
    primary = change.match.get("name") or change.name
    return (primary,) + tuple(change.aliases)
