from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictBool, model_validator

NAME = r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$'


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class Principal(StrictModel):
    tenant_id: str = Field(pattern=NAME)
    actor_id: str = Field(default='configured-actor', pattern=NAME)
    role: Literal['submitter','operator','bridge'] = 'submitter'


class Transition(StrictModel):
    from_state: str = Field(pattern=NAME)
    to_state: str = Field(pattern=NAME)


class Settings(StrictModel):
    states: tuple[str,...] = ('proposed','open','in_progress','done','cancelled')
    initial_status: str = 'proposed'
    transitions: tuple[Transition,...] = tuple(Transition(from_state=a,to_state=b) for a,b in (
        ('proposed','open'),('proposed','cancelled'),('open','in_progress'),('open','cancelled'),
        ('in_progress','done'),('in_progress','cancelled'),('done','open')))
    enabled: StrictBool = True
    assignments: StrictBool = True
    transitions_enabled: StrictBool = True

    @model_validator(mode='after')
    def valid(self):
        import re
        if not 1 <= len(self.states) <= 12 or len(set(self.states)) != len(self.states) or any(not re.fullmatch(NAME,s) for s in self.states):
            raise ValueError('Use 1-12 distinct safe state names')
        if self.initial_status not in self.states or len(self.transitions)>40:
            raise ValueError('Invalid initial state or too many transitions')
        pairs=[(t.from_state,t.to_state) for t in self.transitions]
        if len(set(pairs))!=len(pairs) or any(a==b or a not in self.states or b not in self.states for a,b in pairs):
            raise ValueError('Invalid transition graph')
        return self


class Source(StrictModel):
    kind: Literal['feedback'] = 'feedback'
    id: str = Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    revision: StrictInt = Field(ge=1, le=2147483647)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class Submission(StrictModel):
    title: str = Field(min_length=1,max_length=180)
    description: str = Field(default='',max_length=10000)
    source: Source | None = None

    @model_validator(mode='after')
    def nonblank(self):
        if not self.title.strip():
            raise ValueError('Title cannot be blank')
        return self


class Mutation(StrictModel):
    expected_revision: StrictInt = Field(ge=1,le=2147483647)
    status: str | None = Field(default=None,max_length=100)
    assignee: str | None = Field(default=None,pattern=NAME)


class Item(StrictModel):
    id: str = Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    revision: StrictInt = Field(ge=1,le=2147483647)
    status: str = Field(pattern=NAME)
    title: str = Field(min_length=1,max_length=180)
    description: str = Field(max_length=10000)
    assignee: str | None = Field(pattern=NAME)
    source: Source | None
    created_at: str = Field(max_length=64)
    updated_at: str = Field(max_length=64)


class Acknowledgment(StrictModel):
    status: Literal['acknowledged']
    item: Item
    replayed: StrictBool


class MetadataModel(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)


class EventCost(MetadataModel):
    status:Literal['unknown']
    amount:None
    currency:None


class ChangeEvent(MetadataModel):
    schema:Literal['gradia.work-items.event.v1']
    schema_version:Literal['1']
    event_id:str=Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    block_id:Literal['gradia.work-items']
    block_version:Literal['0.1.0','0.2.0']
    edition:Literal['0.1.0','0.2.0']
    tenant_id:str=Field(pattern=NAME)
    actor_id:str=Field(pattern=NAME)
    item_id:str=Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    revision:int=Field(ge=1,le=2147483647)
    operation:Literal['item.proposed','item.updated']
    result:Literal['persisted']
    correlation_id:str=Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    configuration_digest:str=Field(pattern=r'^[0-9a-f]{64}$')
    duration_ms:int|float=Field(ge=0,allow_inf_nan=False)
    occurred_at:str=Field(max_length=64)
    privacy:Literal['metadata_only']
    acceptance:Literal['not_evaluated']
    billability:Literal['not_evaluated']
    actor_type:Literal['configured_principal']
    human_involvement:Literal['unknown']
    cost:EventCost
    coverage:Literal['committed_mutations_only']


class ChangeRecord(MetadataModel):
    sequence:int=Field(ge=1,le=9007199254740991)
    event:ChangeEvent


class ChangeFeedPage(MetadataModel):
    schema:Literal['gradia.work-items.changefeed.v1']
    journal_id:str=Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    tenant_id:str=Field(pattern=NAME)
    head_sequence:int=Field(ge=0,le=9007199254740991)
    items:list[ChangeRecord]=Field(max_length=100)
    next_after:int=Field(ge=0,le=9007199254740991)
