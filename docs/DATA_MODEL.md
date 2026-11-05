# Data Model

下面是逻辑模型，不要求数据库表名完全一致，但语义必须保留。

## Novel
- id
- title
- genre
- premise
- status
- current_chapter_number
- current_arc_id
- created_at / updated_at

## Character
静态身份。
- id
- novel_id
- name
- aliases[]
- gender
- description
- personality_traits[]
- background
- immutable_notes

## CharacterState
可变状态。
- character_id
- chapter_number
- alive_status
- current_location_id
- realm_id
- subrealm
- physical_condition
- mental_condition
- current_goals[]
- reputation map
- wealth/resource summary

允许保留历史 version。

## CharacterKnowledge
- id
- character_id
- fact_id
- status: known | suspected | believed_false | forgotten
- confidence 0~1
- learned_event_id
- learned_chapter

## Relationship
- source_character_id
- target_character_id
- type
- trust [-100,100]
- affection [-100,100]
- hostility [-100,100]
- debt
- public_description
- hidden_description
- last_changed_event_id

注意 relationship 是有向的。

## StoryFact
任何可被知道/隐藏的命题。
- id
- statement
- truth_status
- secrecy: public | limited | secret | top_secret
- introduced_chapter
- revealed_to_reader_chapter nullable
- source_event_id nullable

## PowerSystem
- id
- name
- description
- rules[]

## Realm
- id
- power_system_id
- name
- rank
- sublevels[]
- requirements
- capabilities

## Ability / Technique
- id
- name
- type
- grade
- requirements
- effects
- limitations[]
- owner/learned_by relation

## Item
- id
- name
- type
- grade
- description
- current_owner_character_id nullable
- current_location_id nullable
- status
- abilities[]
- restrictions[]
- origin
- secret_fact_ids[]
- first_appearance_chapter
- last_appearance_chapter

## Location
- id
- name
- parent_location_id
- description
- faction_id nullable
- travel_metadata
- secret_fact_ids[]

## Faction
- id
- name
- type
- leader_character_id
- description
- goals[]
- status

FactionRelation 可独立成表。

## Event
- id
- novel_id
- chapter_id
- scene_index
- event_type
- title
- summary
- participant_ids[]
- location_id
- importance 0~1
- happened_at_story_time
- evidence_span
- metadata

事件 effects 不要只存在 metadata，可采用 EventEffect 表：
- target_type
- target_id
- field
- operation
- old_value
- new_value

## PlotThread
- id
- title
- type
- status: planned | active | dormant | resolved | abandoned
- truth
- public_summary
- introduced_chapter
- target_resolution_chapter nullable
- priority
- related_entity_ids[]
- last_advanced_chapter

## Foreshadowing
- id
- plot_thread_id
- setup
- intended_payoff
- status: planned | planted | reinforced | paid_off | cancelled
- planted_chapter
- payoff_chapter nullable
- visibility

## PlotDebt
“叙事承诺”。
- id
- debt_type
- description
- created_chapter
- due_story_time / due_chapter nullable
- status: open | fulfilled | broken | cancelled
- related_entities[]

## Constraint
- id
- type: hard | soft
- scope: novel | arc | character | plot_thread
- statement
- active
- created_by: user | system
- start_chapter
- end_chapter nullable

## Arc
- id
- title
- start_chapter
- planned_end_chapter
- goal
- antagonist
- major_beats[]
- character_growth_plan
- mysteries[]
- ending_hook
- status

## Chapter
- id
- novel_id
- number
- version
- title
- status: draft | validated | final | superseded
- body
- user_direction
- chapter_plan_json
- continuity_report_json
- created_at

## ChapterSummary
- chapter_id
- short_summary
- detailed_summary
- key_event_ids[]
- character_ids[]
- plot_thread_ids[]

## StorySnapshot
每章 finalized 后生成。
- id
- novel_id
- after_chapter_number
- state_version
- serialized_state / hash
- created_at

MVP 可保存 JSON snapshot；后续也可以只保存 projection + hash。
