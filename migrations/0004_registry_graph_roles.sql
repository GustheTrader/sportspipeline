-- 0004: ontology registry contents, graph traversal, roles and grants.

-- ---------------------------------------------------------------------------
-- Registry. NewsItem and Fact are 'proposed' until the extraction phase
-- builds their tables.
-- ---------------------------------------------------------------------------

insert into kb.object_type (name, table_name, description, properties, status) values
('Team',          'kb.team',           'A team in a league',                        '["id","league","name","abbreviation"]', 'active'),
('Player',        'kb.player',         'A player',                                  '["id","league","name","position"]', 'active'),
('Game',          'kb.game',           'A scheduled or played game',                '["id","league","home_team_id","away_team_id","start_time","status","home_score","away_score"]', 'active'),
('Market',        'kb.market',         'A priced market on a game at one venue',    '["id","game_id","kind","venue","venue_market_id"]', 'active'),
('PriceSnapshot', 'kb.price_snapshot', 'One observed quote; high volume, never in a graph store', '["market_id","source","quote","side","line","american_odds","observed_at"]', 'active'),
('Injury',        'kb.injury',         'An injury status, valid from report to resolution', '["player_id","team_id","status","reported_at","valid_from","valid_to","source"]', 'active'),
('NewsItem',      null,                'A news article or post (extraction phase)', '["id","source","url","published_at","text","embedding"]', 'proposed'),
('Fact',          null,                'A fact extracted from news, with provenance (extraction phase)', '["subject","predicate","object","valid_from","valid_to","source","confidence"]', 'proposed'),
('Signal',        'kb.signal',         'A pick stamped with its entry price',       '["id","model","market_id","side","entry_american","stamped_at"]', 'active'),
('Grade',         'kb.grade',          'A signal resolved against the final result','["signal_id","outcome","profit_units","clv"]', 'active'),
('Agent',         'kb.agent',          'A bot or human that can request actions',   '["id","name","role"]', 'active'),
('RiskLimit',     'kb.risk_limit',     'A human-owned limit; agents may only tighten it', '["scope","scope_key","max_stake","max_daily_exposure","max_price_age_s","kill_switch"]', 'active'),
('ActionRequest', 'kb.action_request', 'An append-only request and its decision',   '["id","action","params","requested_by","status","decision_reason"]', 'active'),
('Order',         'kb.order_ticket',   'What the executor did for an approved request', '["id","request_id","market_id","side","stake","mode","status"]', 'active');

insert into kb.link_type (name, src_type, dst_type, description, storage, time_bounded, status) values
('PLAYS_FOR',       'Player',        'Team',          'Player on a roster',                 'fk',   true,  'active'),
('PARTICIPATES_IN', 'Team',          'Game',          'Home or away team',                  'fk',   false, 'active'),
('OFFERS',          'Game',          'Market',        'A game has many markets',            'fk',   false, 'active'),
('PRICED_AS',       'Market',        'PriceSnapshot', 'Joined by market id, never traversed','fk',   false, 'active'),
('AFFECTS',         'Injury',        'Player',        'Injury to a player',                 'fk',   true,  'active'),
('AFFECTS',         'Injury',        'Game',          'Injury open at the game''s start',   'fk',   true,  'active'),
('MENTIONS',        'NewsItem',      'Team',          'Written by the extraction job',      'edge', false, 'proposed'),
('MENTIONS',        'NewsItem',      'Player',        'Written by the extraction job',      'edge', false, 'proposed'),
('MENTIONS',        'NewsItem',      'Game',          'Written by the extraction job',      'edge', false, 'proposed'),
('ASSERTS',         'NewsItem',      'Fact',          'Provenance for every extracted fact','edge', false, 'proposed'),
('BECOMES',         'Fact',          'Injury',        'A confirmed fact becomes an injury', 'edge', false, 'proposed'),
('SIGNALS',         'Signal',        'Market',        'The market a signal prices',         'fk',   false, 'active'),
('GRADED_AS',       'Signal',        'Grade',         'Result of a signal',                 'fk',   false, 'active'),
('REQUESTED',       'Agent',         'ActionRequest', 'Who asked',                          'fk',   false, 'active'),
('GOVERNED_BY',     'ActionRequest', 'RiskLimit',     'Which limits were checked',          'fk',   false, 'active'),
('RESULTED_IN',     'ActionRequest', 'Order',         'Only when approved',                 'fk',   false, 'active'),
('TARGETS',         'Order',         'Market',        'What was traded',                    'fk',   false, 'active');

-- ---------------------------------------------------------------------------
-- Graph view: every foreign-key relation plus kb.edge, as one edge list.
-- Price snapshots are left out on purpose (too many, and not entities).
-- ---------------------------------------------------------------------------

create view kb.graph_edge_v with (security_invoker = true) as
select 'Player'::text as src_type, pt.player_id::text as src_id, 'PLAYS_FOR'::text as rel,
       'Team'::text as dst_type, pt.team_id::text as dst_id, pt.valid_from, pt.valid_to
from kb.player_team pt
union all
select 'Team', g.home_team_id::text, 'PARTICIPATES_IN', 'Game', g.id::text, null::timestamptz, null::timestamptz from kb.game g
union all
select 'Team', g.away_team_id::text, 'PARTICIPATES_IN', 'Game', g.id::text, null, null from kb.game g
union all
select 'Game', m.game_id::text, 'OFFERS', 'Market', m.id::text, null, null from kb.market m
union all
select 'Injury', i.id::text, 'AFFECTS', 'Player', i.player_id::text, i.valid_from, i.valid_to from kb.injury i
union all
select 'Injury', v.injury_id::text, 'AFFECTS', 'Game', v.game_id::text, null, null from kb.injury_game_v v
union all
select 'Signal', s.id::text, 'SIGNALS', 'Market', s.market_id::text, null, null from kb.signal s
union all
select 'Signal', gr.signal_id::text, 'GRADED_AS', 'Grade', gr.signal_id::text, null, null from kb.grade gr
union all
select 'Agent', r.requested_by, 'REQUESTED', 'ActionRequest', r.id::text, null, null from kb.action_request r
union all
select 'ActionRequest', c.request_id::text, 'GOVERNED_BY', 'RiskLimit', c.risk_limit_id::text, null, null
from kb.action_check c where c.risk_limit_id is not null
union all
select 'ActionRequest', o.request_id::text, 'RESULTED_IN', 'Order', o.id::text, null, null from kb.order_ticket o
union all
select 'Order', o.id::text, 'TARGETS', 'Market', o.market_id::text, null, null from kb.order_ticket o
union all
select e.src_type, e.src_id, e.rel, e.dst_type, e.dst_id, e.valid_from, e.valid_to from kb.edge e;

-- Multi-hop neighbourhood, both directions, valid at p_at. Recursive CTE.
create function kb.neighbors(p_type text, p_id text, p_depth integer default 2,
                             p_at timestamptz default now())
returns table (depth integer, src_type text, src_id text, rel text, dst_type text, dst_id text)
language sql stable set search_path = '' as $$
    with recursive live as (
        select e.src_type, e.src_id, e.rel, e.dst_type, e.dst_id
        from kb.graph_edge_v e
        where (e.valid_from is null or e.valid_from <= p_at)
          and (e.valid_to is null or e.valid_to > p_at)
    ),
    walk (depth, src_type, src_id, rel, dst_type, dst_id, node_type, node_id, path) as (
        select 1, l.src_type, l.src_id, l.rel, l.dst_type, l.dst_id,
               case when l.src_type = p_type and l.src_id = p_id then l.dst_type else l.src_type end,
               case when l.src_type = p_type and l.src_id = p_id then l.dst_id else l.src_id end,
               array[p_type || ':' || p_id]
        from live l
        where (l.src_type = p_type and l.src_id = p_id) or (l.dst_type = p_type and l.dst_id = p_id)
        union all
        select w.depth + 1, l.src_type, l.src_id, l.rel, l.dst_type, l.dst_id,
               case when l.src_type = w.node_type and l.src_id = w.node_id then l.dst_type else l.src_type end,
               case when l.src_type = w.node_type and l.src_id = w.node_id then l.dst_id else l.src_id end,
               w.path || (w.node_type || ':' || w.node_id)
        from walk w
        join live l on (l.src_type = w.node_type and l.src_id = w.node_id)
                    or (l.dst_type = w.node_type and l.dst_id = w.node_id)
        where w.depth < least(greatest(p_depth, 1), 4)
          and not ((case when l.src_type = w.node_type and l.src_id = w.node_id
                         then l.dst_type || ':' || l.dst_id
                         else l.src_type || ':' || l.src_id end) = any (w.path || (w.node_type || ':' || w.node_id)))
    )
    select distinct min(w.depth) over (partition by w.src_type, w.src_id, w.rel, w.dst_type, w.dst_id),
           w.src_type, w.src_id, w.rel, w.dst_type, w.dst_id
    from walk w
    order by 1, 2, 3, 4;
$$;

-- ---------------------------------------------------------------------------
-- Roles. Group roles are NOLOGIN; each bot gets its own login that is a
-- member of one group (see `sportspipeline agents add`).
-- ---------------------------------------------------------------------------

do $$
declare r text;
begin
    foreach r in array array['sp_agent', 'sp_risk_officer', 'sp_executor', 'sp_human'] loop
        if not exists (select 1 from pg_roles where rolname = r) then
            execute format('create role %I nologin', r);
        end if;
    end loop;
end $$;

-- Belt and braces: nothing in kb is callable or readable unless granted here.
revoke all on all tables in schema kb from public;
revoke all on all functions in schema kb from public;
do $$
declare r text;
begin
    foreach r in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on all tables in schema kb from %I', r);
            execute format('revoke all on all functions in schema kb from %I', r);
            execute format('revoke usage on schema kb from %I', r);
        end if;
    end loop;
end $$;

grant usage on schema kb to sp_agent, sp_risk_officer, sp_executor, sp_human;

-- Read access to the knowledge base. No role gets INSERT/UPDATE/DELETE on any
-- action-layer table: every write goes through a function below.
grant select on
    kb.league, kb.team, kb.team_alias, kb.player, kb.player_team, kb.game, kb.game_source,
    kb.market, kb.price_snapshot, kb.injury, kb.injury_game_v, kb.edge, kb.object_type,
    kb.link_type, kb.signal, kb.grade, kb.ledger_v, kb.agent, kb.risk_limit,
    kb.risk_limit_current, kb.action_type, kb.action_request, kb.action_check, kb.action_event,
    kb.order_ticket, kb.ingest_run, kb.graph_edge_v
to sp_agent, sp_risk_officer, sp_executor, sp_human;

grant execute on function kb.propose_action(text, jsonb) to sp_agent, sp_risk_officer;
grant execute on function kb.size_stake(bigint, text, numeric, numeric, numeric) to sp_agent, sp_risk_officer;
grant execute on function kb.neighbors(text, text, integer, timestamptz)
    to sp_agent, sp_risk_officer, sp_executor, sp_human;
grant execute on function kb.claim_approved(integer) to sp_executor;
grant execute on function kb.record_execution(uuid, boolean, text, text, text, text, jsonb, jsonb, text)
    to sp_executor;
grant execute on function kb.set_risk_limit_human(text, text, numeric, numeric, integer, boolean, text)
    to sp_human;

-- ---------------------------------------------------------------------------
-- Row level security: an agent sees only its own requests, checks, events
-- and orders. Officers, executors and humans see everything. The table owner
-- (the migration role) is not subject to these policies.
-- ---------------------------------------------------------------------------

alter table kb.action_request enable row level security;
alter table kb.action_check   enable row level security;
alter table kb.action_event   enable row level security;
alter table kb.order_ticket   enable row level security;

create function kb._is_my_request(p_request uuid) returns boolean
language sql stable security definer set search_path = '' as $$
    select exists (select 1 from kb.action_request r
                   join kb.agent a on a.id = r.requested_by
                   where r.id = p_request and a.db_role = session_user);
$$;
grant execute on function kb._is_my_request(uuid) to sp_agent;

create policy own_requests on kb.action_request for select to sp_agent
    using (kb._is_my_request(id));
create policy own_checks on kb.action_check for select to sp_agent
    using (kb._is_my_request(request_id));
create policy own_events on kb.action_event for select to sp_agent
    using (kb._is_my_request(request_id));
create policy own_orders on kb.order_ticket for select to sp_agent
    using (kb._is_my_request(request_id));

create policy all_requests on kb.action_request for select to sp_risk_officer, sp_executor, sp_human using (true);
create policy all_checks   on kb.action_check   for select to sp_risk_officer, sp_executor, sp_human using (true);
create policy all_events   on kb.action_event   for select to sp_risk_officer, sp_executor, sp_human using (true);
create policy all_orders   on kb.order_ticket   for select to sp_risk_officer, sp_executor, sp_human using (true);
