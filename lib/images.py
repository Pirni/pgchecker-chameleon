"""Image lookup for python-chi that accepts UUIDs and community images.

python-chi resolves an image by name, with a Glance name filter, in two places:
`chi.image.get_image` (Server constructor, also when python-chi rebuilds a Server
for an existing instance, e.g. in `server.get_server` before a delete) and
`chi.server.get_image_id` (server create in `Server.submit`). Glance's default
listing covers public, private and shared images; community images of other
projects appear with `visibility=all`.

install() wraps both lookups:
- a UUID is used as-is (checked once with `images.get`),
- a name is looked up as before, then with `visibility=all`.
"""

from __future__ import annotations

import uuid

from chi import image as chi_image
from chi import server as chi_server
from chi.clients import glance
from chi.exception import CHIValueError


def is_uuid(ref: str) -> bool:
    try:
        uuid.UUID(str(ref))
    except ValueError:
        return False
    return True


def _find_by_name_any_visibility(name: str):
    images = list(glance().images.list(filters={"name": name, "visibility": "all"}))
    if not images:
        raise CHIValueError(f'No images found matching name "{name}"')
    return images[0]


def install() -> None:
    """Wrap python-chi's image lookups (idempotent)."""
    if getattr(chi_image.get_image, "_uuid_aware", False):
        return

    original_get_image = chi_image.get_image
    original_get_image_id = chi_server.get_image_id

    def get_image(ref: str) -> chi_image.Image:
        if is_uuid(ref):
            glance_image = glance().images.get(ref)
            # name = UUID, so Server.submit boots exactly this image.
            return chi_image.Image(
                uuid=glance_image.id,
                created_at=glance_image.created_at,
                is_chameleon_supported=False,
                name=glance_image.id,
            )
        try:
            return original_get_image(ref)
        except CHIValueError:
            return chi_image.Image.from_glance_image(_find_by_name_any_visibility(ref))

    def get_image_id(ref: str) -> str:
        if is_uuid(ref):
            return ref
        try:
            return original_get_image_id(ref)
        except CHIValueError:
            return _find_by_name_any_visibility(ref).id

    get_image._uuid_aware = True
    chi_image.get_image = get_image
    chi_server.get_image_id = get_image_id
