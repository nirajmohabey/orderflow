{% macro checked_uuid(value) -%}
  {% set value = value|string|lower %}
  {% set compact = value|replace('-', '') %}
  {% if value|length != 36 or compact|length != 32 %}
    {{ exceptions.raise_compiler_error('A validated UUID is required') }}
  {% endif %}
  {% for letter in compact %}
    {% if letter not in '0123456789abcdef' %}
      {{ exceptions.raise_compiler_error('A validated UUID is required') }}
    {% endif %}
  {% endfor %}
  {{ return(value) }}
{%- endmacro %}
{% macro candidate_alias(name) -%}
  {{ return(name ~ '_' ~ checked_uuid(var('build_id',var('run_id')))|replace('-', '')) }}
{%- endmacro %}
