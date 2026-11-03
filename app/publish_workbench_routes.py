"""Authenticated business-form API; observed platform evidence is worker-only."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from . import publish_workbench as workbench
from .security import require_user


router = APIRouter(prefix='/api', dependencies=[Depends(require_user)])


class StrictInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class NewGroup(StrictInput):
    product_ids: list[str] = Field(min_length=1, max_length=100)
    store_ids: list[str] = Field(min_length=1, max_length=30)
    mode: Literal['create', 'update'] = 'create'


class NewDraft(StrictInput):
    product_id: str = Field(min_length=1, max_length=100)
    store_ids: list[str] = Field(min_length=1, max_length=30)
    mode: Literal['create', 'update'] = 'create'


class SaveDraft(StrictInput):
    revision: int = Field(strict=True, ge=1)
    common_values: dict = Field(default_factory=dict)
    store_values: dict[str, dict] = Field(default_factory=dict)


class DraftAction(StrictInput):
    revision: int | None = Field(default=None, strict=True, ge=1)
    upload_images: bool = Field(default=False, strict=True)


class GroupAction(StrictInput):
    revisions: dict[str, int] | None = None
    upload_images: bool = Field(default=False, strict=True)


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'")) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post('/publish-groups')
def create_group(body: NewGroup):
    return call(workbench.create, body.product_ids, body.store_ids, body.mode)


@router.get('/publish-groups')
def list_groups(limit: int = Query(default=20, ge=1, le=50), offset: int = Query(default=0, ge=0)):
    return call(workbench.list_groups, limit, offset)


@router.get('/publish-groups/{group_id}')
def get_group(group_id: str):
    return call(workbench.get_group, group_id)


@router.delete('/publish-groups/{group_id}')
def delete_group(group_id: str):
    return call(workbench.delete_group, group_id)


@router.post('/publish-groups/{group_id}/retry-failed')
def retry_failed(group_id: str, body: GroupAction):
    return call(workbench.retry_failed, group_id, body.upload_images)


@router.post('/publish-groups/{group_id}/audit')
def audit_group(group_id: str):
    return call(workbench.audit_group, group_id)


@router.post('/publish-groups/{group_id}/{action}')
def group_action(group_id: str, action: str, body: GroupAction):
    if action not in ('collect', 'prepare', 'submit'):
        raise HTTPException(404, '未知统一发布操作')
    if action in ('prepare', 'submit') and body.revisions is None:
        raise HTTPException(422, '请提供当前表单版本后再确认操作')
    return call(workbench.queue_group, group_id, action, body.revisions, body.upload_images)


@router.post('/publish-drafts')
def create_draft(body: NewDraft):
    return call(workbench.create, [body.product_id], body.store_ids, body.mode)['drafts'][0]


@router.get('/publish-drafts/{draft_id}')
def get_draft(draft_id: str):
    return call(workbench.get, draft_id)


@router.put('/publish-drafts/{draft_id}')
def save_draft(draft_id: str, body: SaveDraft):
    return call(workbench.save, draft_id, body.revision, body.common_values, body.store_values)


@router.delete('/publish-drafts/{draft_id}')
def delete_draft(draft_id: str):
    return call(workbench.delete, draft_id)


@router.post('/publish-drafts/{draft_id}/{action}')
def draft_action(draft_id: str, action: str, body: DraftAction):
    if action not in ('collect', 'prepare', 'submit'):
        raise HTTPException(404, '未知统一发布操作')
    if action in ('prepare', 'submit') and body.revision is None:
        raise HTTPException(422, '请提供当前表单版本后再确认操作')
    revisions = {draft_id: body.revision} if body.revision is not None else None
    return call(workbench.queue, [draft_id], action, revisions, body.upload_images)
