"""
Unit tests for custom_update dependency ordering (Tier J):
- the Phase 0a dependency-order validator in fleet-update.yml
- the runtime _dep_failed gate in roles/custom_update/tasks/main.yml
"""
from tests.conftest import make_native_env

# Phase 0a validator (builds a list of ordering problems)
DEP_PROBLEMS = """\
{%- set chosts = groups['custom_hosts'] | default([]) -%}
{%- set probs = [] -%}
{%- for h in chosts -%}
{%- for d in hostvars[h].depends_on | default([]) -%}
{%- if d not in chosts -%}
{%- set _ = probs.append(h ~ ' depends_on missing custom host ' ~ d) -%}
{%- elif chosts.index(d) > chosts.index(h) -%}
{%- set _ = probs.append(h ~ ' depends_on ' ~ d ~ ' which is listed AFTER it') -%}
{%- endif -%}
{%- endfor -%}
{%- endfor -%}
{{ probs }}"""

# Runtime gate
DEP_FAILED = """\
{{ ((depends_on | default([]))
    | intersect((fleet_custom_data | default([]))
                | selectattr('app', 'search', 'FAILED') | map(attribute='host') | list))
   | length > 0 }}"""


def rn(expr, **ctx):
    return make_native_env().from_string(expr).render(**ctx)


def problems(chosts, hostvars):
    return rn(DEP_PROBLEMS, groups={'custom_hosts': chosts}, hostvars=hostvars)


# --- Phase 0a ordering validator ---

def test_no_problems_when_no_depends():
    assert problems(['a', 'b'], {'a': {}, 'b': {}}) == []


def test_no_problems_when_dependency_listed_first():
    assert problems(['db', 'app'], {'db': {}, 'app': {'depends_on': ['db']}}) == []


def test_problem_when_dependency_listed_after():
    probs = problems(['app', 'db'], {'app': {'depends_on': ['db']}, 'db': {}})
    assert len(probs) == 1
    assert 'listed AFTER' in probs[0]


def test_problem_when_dependency_missing():
    probs = problems(['app'], {'app': {'depends_on': ['ghost']}})
    assert len(probs) == 1
    assert 'missing custom host' in probs[0]


# --- runtime _dep_failed gate ---

def test_dep_failed_true_when_dependency_failed():
    fcd = [{'host': 'db-01', 'name': 'pg', 'app': 'FAILED'}]
    assert rn(DEP_FAILED, depends_on=['db-01'], fleet_custom_data=fcd) is True


def test_dep_failed_false_when_dependency_ok():
    fcd = [{'host': 'db-01', 'name': 'pg', 'app': 'Updated: 14 → 15'}]
    assert rn(DEP_FAILED, depends_on=['db-01'], fleet_custom_data=fcd) is False


def test_dep_failed_false_when_no_depends():
    assert rn(DEP_FAILED, depends_on=[], fleet_custom_data=[]) is False
