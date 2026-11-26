-- 0001: schema, ontology registry, ingest runs, structured entities.
--
-- Everything lives in schema `kb`, which is not one of the schemas Supabase
-- exposes through its REST API. Functions created here default to no EXECUTE
-- for PUBLIC; each grant is explicit (see 0005).

create schema if not exists kb;
revoke all on schema kb from public;
alter default privileges in schema kb revoke execute on functions from public;

create schema if not exists extensions;
create extension if not exists pg_jsonschema with schema extensions;

-- ---------------------------------------------------------------------------
-- Ontology registry: the semantic layer's source of truth.
-- ---------------------------------------------------------------------------

create table kb.object_type (
    name        text primary key,
    table_name  text,                       -- typed table backing the type, if any
    description text not null,
    properties  jsonb not null default '[]'::jsonb,
    status      text not null default 'active'
                check (status in ('active', 'proposed', 'retired')),
    created_at  timestamptz not null default now()
);

create table kb.link_type (
    name         text not null,
    src_type     text not null references kb.object_type(name),
    dst_type     text not null references kb.object_type(name),
    description  text not null,
    -- 'fk'   : the relation is a foreign key between typed tables
    -- 'edge' : the relation is stored as rows in kb.edge
    storage      text not null check (storage in ('fk', 'edge')),
    time_bounded boolean not null default false,
    status       text not null default 'active'
                 check (status in ('active', 'proposed', 'retired')),
    created_at   timestamptz not null default now(),
    primary key (name, src_type, dst_type)
);

-- ---------------------------------------------------------------------------
-- Ingest runs: every pull is recorded, so an empty table can be told apart
-- from a failed fetch.
-- ---------------------------------------------------------------------------

create table kb.league (
    code      text primary key,
    sport     text not null,
    espn_path text not null unique,
    name      text not null
);

insert into kb.league (code, sport, espn_path, name) values
    ('nfl',   'football',   'football/nfl',                     'NFL'),
    ('ncaaf', 'football',   'football/college-football',        'NCAA Football'),
    ('nba',   'basketball', 'basketball/nba',                   'NBA'),
    ('wnba',  'basketball', 'basketball/wnba',                  'WNBA'),
    ('ncaab', 'basketball', 'basketball/mens-college-basketball','NCAA Men''s Basketball'),
    ('nhl',   'hockey',     'hockey/nhl',                       'NHL'),
    ('mlb',   'baseball',   'baseball/mlb',                     'MLB');

create table kb.ingest_run (
    id            bigint generated always as identity primary key,
    source        text not null,
    kind          text not null,           -- teams | scoreboard | injuries | ...
    league        text references kb.league(code),
    window_label  text,                    -- e.g. the dates= value requested
    request_url   text,
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    status        text not null default 'running'
                  check (status in ('running', 'ok', 'empty', 'failed')),
    rows_seen     integer not null default 0,
    rows_written  integer not null default 0,
    unresolved    integer not null default 0,
    error         text,
    check ((status = 'running') = (finished_at is null)),
    check (status <> 'failed' or error is not null)
);

create index ingest_run_recent on kb.ingest_run (source, kind, league, started_at desc);

-- ---------------------------------------------------------------------------
-- Teams and the alias table. A new vendor spelling is a row, not a code branch.
-- ---------------------------------------------------------------------------

create table kb.team (
    id           bigint generated always as identity primary key,
    league       text not null references kb.league(code),
    name         text not null,
    abbreviation text,
    created_at   timestamptz not null default now(),
    unique (league, name)
);

create table kb.team_alias (
    source     text not null,
    league     text not null references kb.league(code),
    alias      text not null check (alias = lower(btrim(alias)) and alias <> ''),
    team_id    bigint not null references kb.team(id),
    created_at timestamptz not null default now(),
    primary key (source, league, alias)
);

-- Names we saw but could not map. Visible, counted, never guessed.
create table kb.unresolved_name (
    source        text not null,
    league        text not null references kb.league(code),
    kind          text not null check (kind in ('team', 'player')),
    raw_name      text not null,
    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    seen_count    integer not null default 1,
    last_run_id   bigint references kb.ingest_run(id),
    primary key (source, league, kind, raw_name)
);

-- ---------------------------------------------------------------------------
-- Players (PLAYS_FOR is time-bounded).
-- ---------------------------------------------------------------------------

create table kb.player (
    id         bigint generated always as identity primary key,
    league     text not null references kb.league(code),
    name       text not null,
    position   text,
    created_at timestamptz not null default now()
);

create table kb.player_source (
    source      text not null,
    external_id text not null,
    player_id   bigint not null references kb.player(id),
    primary key (source, external_id)
);

create table kb.player_team (
    player_id  bigint not null references kb.player(id),
    team_id    bigint not null references kb.team(id),
    valid_from timestamptz not null,
    valid_to   timestamptz,
    source     text not null,
    primary key (player_id, valid_from),
    check (valid_to is null or valid_to > valid_from)
);
create unique index player_team_one_open on kb.player_team (player_id) where valid_to is null;

-- ---------------------------------------------------------------------------
-- Games, markets, price snapshots.
-- ---------------------------------------------------------------------------

create table kb.game (
    id           bigint generated always as identity primary key,
    league       text not null references kb.league(code),
    home_team_id bigint not null references kb.team(id),
    away_team_id bigint not null references kb.team(id),
    start_time   timestamptz not null,
    status       text not null
                 check (status in ('scheduled', 'in_progress', 'final', 'postponed', 'canceled')),
    home_score   integer,
    away_score   integer,
    updated_at   timestamptz not null default now(),
    check (home_team_id <> away_team_id),
    check (status <> 'final' or (home_score is not null and away_score is not null))
);
create index game_start on kb.game (league, start_time);

create table kb.game_source (
    source      text not null,
    external_id text not null,
    game_id     bigint not null references kb.game(id),
    primary key (source, external_id),
    unique (source, game_id)
);

create table kb.market (
    id              bigint generated always as identity primary key,
    game_id         bigint not null references kb.game(id),
    kind            text not null check (kind in ('moneyline', 'spread', 'total')),
    venue           text not null,          -- the book or exchange quoting it
    venue_market_id text,
    created_at      timestamptz not null default now(),
    unique (game_id, kind, venue)
);

-- One row per observed quote. Rows are only written when a quote changes, so
-- re-running an ingest over the same data writes nothing.
create table kb.price_snapshot (
    id            bigint generated always as identity primary key,
    market_id     bigint not null references kb.market(id),
    source        text not null,           -- the feed we read it from
    -- open    : the opening line as reported by the feed
    -- current : the live pregame quote at observed_at
    -- close   : the quote the feed reports after the game left 'scheduled'
    quote         text not null check (quote in ('open', 'current', 'close')),
    side          text not null check (side in ('home', 'away', 'over', 'under')),
    line          numeric,
    american_odds integer not null check (american_odds <= -100 or american_odds >= 100),
    decimal_odds  numeric generated always as (
                      case when american_odds > 0 then 1 + american_odds / 100.0
                           else 1 + 100.0 / (-american_odds) end
                  ) stored,
    observed_at   timestamptz not null,
    run_id        bigint references kb.ingest_run(id),
    unique (market_id, source, quote, side, observed_at)
);
create index price_snapshot_latest on kb.price_snapshot (market_id, side, quote, observed_at desc);

-- ---------------------------------------------------------------------------
-- Injuries: valid from report to resolution. AFFECTS Player directly and
-- AFFECTS Game through kb.injury_game_v.
-- ---------------------------------------------------------------------------

create table kb.injury (
    id            bigint generated always as identity primary key,
    player_id     bigint not null references kb.player(id),
    team_id       bigint not null references kb.team(id),
    status        text not null,
    detail        text,
    reported_at   timestamptz,
    valid_from    timestamptz not null,
    valid_to      timestamptz,
    source        text not null,
    source_ref    text,
    run_id        bigint references kb.ingest_run(id),
    closed_run_id bigint references kb.ingest_run(id),
    check (valid_to is null or valid_to >= valid_from)
);
create unique index injury_one_open on kb.injury (player_id, source) where valid_to is null;

create view kb.injury_game_v as
select i.id as injury_id, g.id as game_id, i.player_id, i.team_id, i.status
from kb.injury i
join kb.game g on i.team_id in (g.home_team_id, g.away_team_id)
where i.valid_from <= g.start_time
  and (i.valid_to is null or i.valid_to > g.start_time);

-- ---------------------------------------------------------------------------
-- Generic edge table for relations that are not foreign keys. The foreign key
-- to kb.link_type means an edge can only use a registered relation.
-- ---------------------------------------------------------------------------

create table kb.edge (
    id          bigint generated always as identity primary key,
    src_type    text not null,
    src_id      text not null,
    rel         text not null,
    dst_type    text not null,
    dst_id      text not null,
    valid_from  timestamptz not null default now(),
    valid_to    timestamptz,
    source_kind text not null,             -- 'ingest' | 'extraction' | 'human' | ...
    source_id   text,
    confidence  numeric check (confidence is null or confidence between 0 and 1),
    created_at  timestamptz not null default now(),
    foreign key (rel, src_type, dst_type) references kb.link_type(name, src_type, dst_type),
    check (valid_to is null or valid_to > valid_from)
);
create index edge_src on kb.edge (src_type, src_id);
create index edge_dst on kb.edge (dst_type, dst_id);

create function kb.edge_check_link() returns trigger
language plpgsql set search_path = '' as $$
declare
    v_storage text;
    v_status  text;
begin
    select storage, status into v_storage, v_status
    from kb.link_type
    where name = new.rel and src_type = new.src_type and dst_type = new.dst_type;
    if v_storage <> 'edge' then
        raise exception 'relation % is stored as a foreign key, not in kb.edge', new.rel;
    end if;
    if v_status <> 'active' then
        raise exception 'relation % is %, not active', new.rel, v_status;
    end if;
    return new;
end $$;

create trigger edge_check_link before insert or update on kb.edge
for each row execute function kb.edge_check_link();
