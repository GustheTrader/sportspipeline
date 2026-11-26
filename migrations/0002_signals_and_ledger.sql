-- 0002: signals, rejections, grades and the ledger.
--
-- Non-negotiables enforced here, in the database:
--   * no signal without a stamped price: a signal must point at a price
--     snapshot observed before the game started; entry price and stamp are
--     copied from that snapshot, never typed in by the caller
--   * no grade against a game the pick does not belong to
--   * ledger tables are append-only

create function kb.forbid_change() returns trigger
language plpgsql set search_path = '' as $$
begin
    raise exception '% is append-only (% refused)', tg_table_name, tg_op
        using errcode = 'P0001';
end $$;

create table kb.signal (
    id                bigint generated always as identity primary key,
    created_at        timestamptz not null default now(),
    model             text not null,
    created_by        text not null,
    game_id           bigint not null references kb.game(id),
    market_id         bigint not null references kb.market(id),
    side              text not null check (side in ('home', 'away', 'over', 'under')),
    team_id           bigint references kb.team(id),
    price_snapshot_id bigint not null references kb.price_snapshot(id),
    -- copied from the snapshot by the trigger below
    line              numeric,
    entry_american    integer not null,
    entry_decimal     numeric not null,
    stamped_at        timestamptz not null,
    model_prob        numeric check (model_prob is null or model_prob between 0 and 1),
    stake_units       numeric not null default 1 check (stake_units > 0),
    note              text
);
create index signal_game on kb.signal (game_id);

create table kb.signal_rejection (
    id          bigint generated always as identity primary key,
    rejected_at timestamptz not null default now(),
    reason      text not null,
    payload     jsonb not null
);

create function kb.signal_stamp() returns trigger
language plpgsql set search_path = '' as $$
declare
    s  kb.price_snapshot%rowtype;
    m  kb.market%rowtype;
    g  kb.game%rowtype;
    implied_team bigint;
begin
    select * into s from kb.price_snapshot where id = new.price_snapshot_id;
    if not found then
        raise exception 'signal needs a price snapshot' using errcode = 'SP001';
    end if;
    select * into m from kb.market where id = s.market_id;
    if new.market_id is distinct from m.id then
        raise exception 'snapshot % belongs to market %, not %', s.id, m.id, new.market_id
            using errcode = 'SP001';
    end if;
    select * into g from kb.game where id = m.game_id;
    if new.game_id is distinct from g.id then
        raise exception 'market % belongs to game %, not %', m.id, g.id, new.game_id
            using errcode = 'SP002';
    end if;
    if s.side <> new.side then
        raise exception 'snapshot side % does not match signal side %', s.side, new.side
            using errcode = 'SP001';
    end if;
    if s.quote = 'close' or s.observed_at >= g.start_time then
        raise exception 'snapshot % was observed at %, not before game start %',
            s.id, s.observed_at, g.start_time using errcode = 'SP001';
    end if;

    if m.kind in ('moneyline', 'spread') then
        implied_team := case new.side when 'home' then g.home_team_id
                                      when 'away' then g.away_team_id end;
        if implied_team is null then
            raise exception 'side % is not valid for a % market', new.side, m.kind
                using errcode = 'SP001';
        end if;
        if new.team_id is not null and new.team_id not in (g.home_team_id, g.away_team_id) then
            raise exception 'team % is not in game %', new.team_id, g.id using errcode = 'SP002';
        end if;
        if new.team_id is not null and new.team_id <> implied_team then
            raise exception 'team % is not the % side of game %', new.team_id, new.side, g.id
                using errcode = 'SP002';
        end if;
        new.team_id := implied_team;
    else
        if new.side not in ('over', 'under') then
            raise exception 'side % is not valid for a total market', new.side using errcode = 'SP001';
        end if;
        if new.team_id is not null then
            raise exception 'a total has no team' using errcode = 'SP002';
        end if;
    end if;

    new.line           := s.line;
    new.entry_american := s.american_odds;
    new.entry_decimal  := s.decimal_odds;
    new.stamped_at     := s.observed_at;
    new.created_at     := now();
    return new;
end $$;

create trigger signal_stamp before insert on kb.signal
for each row execute function kb.signal_stamp();
create trigger signal_append_only before update or delete on kb.signal
for each row execute function kb.forbid_change();
create trigger signal_rejection_append_only before update or delete on kb.signal_rejection
for each row execute function kb.forbid_change();

create table kb.grade (
    signal_id         bigint primary key references kb.signal(id),
    outcome           text not null check (outcome in ('win', 'loss', 'push', 'void')),
    graded_at         timestamptz not null default now(),
    home_score        integer,
    away_score        integer,
    profit_units      numeric not null,
    close_snapshot_id bigint references kb.price_snapshot(id),
    -- closing line value: entry decimal odds / closing decimal odds - 1
    clv               numeric,
    result_source     text not null
);

create function kb.grade_check() returns trigger
language plpgsql set search_path = '' as $$
declare
    sg kb.signal%rowtype;
    g  kb.game%rowtype;
begin
    select * into sg from kb.signal where id = new.signal_id;
    select * into g from kb.game where id = sg.game_id;
    if sg.team_id is not null and sg.team_id not in (g.home_team_id, g.away_team_id) then
        raise exception 'signal % picks a team that is not in game %', sg.id, g.id
            using errcode = 'SP002';
    end if;
    if new.outcome <> 'void' and g.status <> 'final' then
        raise exception 'game % is %, not final', g.id, g.status using errcode = 'SP003';
    end if;
    return new;
end $$;

create trigger grade_check before insert on kb.grade
for each row execute function kb.grade_check();
create trigger grade_append_only before update or delete on kb.grade
for each row execute function kb.forbid_change();

-- Reporting reads the ledger; nothing reports from live feeds.
create view kb.ledger_v with (security_invoker = true) as
select s.id               as signal_id,
       s.created_at,
       s.model,
       s.created_by,
       g.league,
       g.id               as game_id,
       gs.external_id     as espn_event_id,
       g.start_time,
       ht.name            as home_team,
       at.name            as away_team,
       m.kind             as market_kind,
       m.venue,
       s.side,
       t.name             as pick_team,
       s.line,
       s.entry_american,
       s.entry_decimal,
       s.stamped_at,
       s.model_prob,
       s.stake_units,
       gr.outcome,
       gr.home_score,
       gr.away_score,
       gr.profit_units,
       gr.clv,
       gr.graded_at
from kb.signal s
join kb.game g        on g.id = s.game_id
join kb.market m      on m.id = s.market_id
join kb.team ht       on ht.id = g.home_team_id
join kb.team at       on at.id = g.away_team_id
left join kb.team t   on t.id = s.team_id
left join kb.game_source gs on gs.game_id = g.id and gs.source = 'espn'
left join kb.grade gr on gr.signal_id = s.id;
