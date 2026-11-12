"""What an image store holding a kew-built agent image answers about it.

Every agent image kew builds carries the digest of the inputs it was built
from as the image label :data:`AGENT_IMAGE_INPUTS_LABEL`, and a launch of a
``docker-<12 hex>`` pin tag whose label does not name the same inputs is
refused (#3160). A fake standing in for the image store therefore answers the
way a store holding a kew-built image does: the ID it was asked for and, for a
pin tag, a full 64-hex digest starting with the tag's 12 hex.
"""

from __future__ import annotations

from typing import Any

from kew.image_inputs import AGENT_IMAGE_INPUTS_LABEL
from kew.sandbox import AGENT_IMAGE_PIN_TAG_RE, ImageIdStatus, ImageResolution

__all__ = [
    "AGENT_IMAGE_INPUTS_LABEL",
    "built_by_kew",
    "docker_image_probe",
    "inspect_config",
    "instance_image_probe",
    "instance_probe_answer",
    "pinned_inputs",
]

#: What the remote image probe's ``--format`` names when it asks for the
#: label bare, rather than for the ID alone.
_LABEL_FORMAT = ".Config.Labels"
#: What a probe format names when it reads the labels only where they exist.
_GUARDED_FORMAT = '.Config "Labels"'


def pinned_inputs(image: str) -> str | None:
    """The inputs digest a kew build of *image* carries, or None.

    None when *image* is not a kew pin tag: only a pin tag names inputs.
    """
    match = AGENT_IMAGE_PIN_TAG_RE.fullmatch(image)
    return None if match is None else match.group(2) + "0" * 52


def built_by_kew(image: str, image_id: str) -> ImageResolution:
    """*image* resolved to *image_id*, labelled with the inputs its tag names."""
    return ImageResolution(
        ImageIdStatus.RESOLVED, image_id=image_id, inputs_digest=pinned_inputs(image)
    )


def inspect_config(image: str) -> dict[str, Any]:
    """The ``Config`` of ``image inspect`` for a kew build of *image*."""
    inputs = pinned_inputs(image)
    return {"Labels": None if inputs is None else {AGENT_IMAGE_INPUTS_LABEL: inputs}}


def instance_probe_answer(image: str, image_id: str, inputs: str | None = None) -> str:
    """What a remote instance prints for kew's label probe of *image*.

    kew's ``--format`` prints ``<id> inputs=<the inputs label>``, and nothing
    after ``inputs=`` for an image without the label. The label is the one a
    kew build of *image* carries unless *inputs* says what the instance's
    image carries instead ("" for none).
    """
    label = pinned_inputs(image) if inputs is None else inputs
    return f"{image_id} inputs={label or ''}"


#: Docker 29.8's error for ``{{index .Config.Labels "..."}}`` on an image whose
#: ``Config`` has no ``Labels`` key at all (``postgres:16``, an image built
#: before kew labelled its builds).
_NO_LABELS_KEY = (
    'template parsing error: template: :1:30: executing "" at <.Config.Labels>: '
    'map has no entry for key "Labels"\n'
)


def docker_image_probe(image_id: str, labels: dict[str, str] | None) -> dict[str, Any]:
    """A fake gcloud job answer: what Docker 29.8 prints for ``image inspect
    --format`` on an image with *labels* (``None``: no ``Labels`` key at all).

    It answers the format it is asked, as the real daemon does (checked against
    ``postgres:16``, ``caddy:2-alpine`` and an unlabelled kew pin):

    - ``{{.Id}}`` alone prints the ID;
    - kew's guarded format (``with index .Config "Labels"``) prints
      ``<id> inputs=`` and the label when there is one, nothing when not;
    - a bare ``index .Config.Labels "<key>"`` fails the template, exit 1, when
      there is no ``Labels`` key, and prints ``<no value>`` when the key is
      missing from labels the image does have.
    """
    label = (labels or {}).get(AGENT_IMAGE_INPUTS_LABEL)
    if labels is None:
        bare: Any = {"output": "", "exit": 1, "stderr": _NO_LABELS_KEY}
    else:
        bare = f"{image_id} inputs={'<no value>' if label is None else label}"
    return {
        "match": "image inspect",
        "output": image_id,
        "output_if": [
            [_GUARDED_FORMAT, f"{image_id} inputs={label or ''}"],
            [_LABEL_FORMAT, bare],
        ],
        "exit": 0,
    }


def instance_image_probe(image: str, image_id: str) -> dict[str, Any]:
    """A fake gcloud job answer for a remote instance holding a kew build of
    *image*: the ID alone for ``{{.Id}}``, the ID and the label a pin tag's
    build carries for a format that reads the labels (:func:`docker_image_probe`).
    """
    inputs = pinned_inputs(image)
    labels = None if inputs is None else {AGENT_IMAGE_INPUTS_LABEL: inputs}
    return docker_image_probe(image_id, labels)
