#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging

from django.core.urlresolvers import reverse
from django.http import HttpResponse, Http404
from django.utils.translation import ugettext as _

from thrift.transport.TTransport import TTransportException

from desktop.context_processors import get_app_name

from jobsub.parameterization import substitute_variables

import beeswax.models

from beeswax.forms import QueryForm
from beeswax.design import HQLdesign
from beeswax.server import dbms
from beeswax.server.dbms import expand_exception, get_query_server_config
from beeswax.views import authorized_get_design, authorized_get_history, make_parameterization_form,\
                          safe_get_design, save_design, massage_columns_for_json, _get_query_handle_and_state


LOG = logging.getLogger(__name__)


def error_handler(view_fn):
  def decorator(*args, **kwargs):
    try:
      return view_fn(*args, **kwargs)
    except Http404, e:
      raise e
    except Exception, e:
      response = {
        'error': str(e)
      }
      return HttpResponse(json.dumps(response), mimetype="application/json", status=500)
  return decorator


@error_handler
def autocomplete(request, database=None, table=None):
  app_name = get_app_name(request)
  query_server = get_query_server_config(app_name)
  db = dbms.get(request.user, query_server)
  response = {}

  try:
    if database is None:
      response['databases'] = db.get_databases()
    elif table is None:
      response['tables'] = db.get_tables(database=database)
    else:
      t = db.get_table(database, table)
      response['columns'] = [column.name for column in t.cols]
      response['extended_columns'] = massage_columns_for_json(t.cols)
  except TTransportException, tx:
    response['code'] = 503
    response['error'] = tx.message
  except Exception, e:
    LOG.warn('Autocomplete data fetching error %s.%s: %s' % (database, table, e))
    response['code'] = 500
    response['error'] = e.message

  return HttpResponse(json.dumps(response), mimetype="application/json")


@error_handler
def parameters(request, design_id=None):
  response = {'status': -1, 'message': ''}

  # Use POST request to not confine query length.
  if request.method != 'POST':
    response['message'] = _('A POST request is required.')

  parameterization_form_cls = make_parameterization_form(request.POST.get('query-query', ''))
  if parameterization_form_cls:
    parameterization_form = parameterization_form_cls(prefix="parameterization")

    response['parameters'] = [{'parameter': field.html_name, 'name': field.name} for field in parameterization_form]
    response['status']= 0
  else:
    response['parameters'] = []
    response['status']= 0

  return HttpResponse(json.dumps(response), mimetype="application/json")


def execute_directly(request, query, design, query_server, tablename=None, **kwargs):
  if design is not None:
    design = authorized_get_design(request, design.id)

  db = dbms.get(request.user, query_server)
  database = query.query.get('database', 'default')
  db.use(database)

  history_obj = db.execute_query(query, design)
  watch_url = reverse(get_app_name(request) + ':watch_query_refresh_json', kwargs={'id': history_obj.id})

  response = {
    'status': 0,
    'id': history_obj.id,
    'watch_url': watch_url
  }

  return HttpResponse(json.dumps(response), mimetype="application/json")


def explain_directly(request, query, design, query_server):
  explanation = dbms.get(request.user, query_server).explain(query)
  
  response = {
    'status': 0,
    'explanation': explanation.textual
  }

  return HttpResponse(json.dumps(response), mimetype="application/json")


@error_handler
def execute(request, query_id=None):
  response = {'status': -1, 'message': ''}

  if request.method != 'POST':
    response['message'] = _('A POST request is required.')
  
  app_name = get_app_name(request)
  query_server = get_query_server_config(app_name)
  query_type = beeswax.models.SavedQuery.TYPES_MAPPING[app_name]
  design = safe_get_design(request, query_type, query_id)

  try:
    query_form = get_query_form(request)

    if query_form.is_valid():
      query_str = query_form.query.cleaned_data["query"]
      explain = request.GET.get('explain', 'false').lower() == 'true'
      design = save_design(request, query_form, query_type, design, False)

      if query_form.query.cleaned_data['is_parameterized']:
        # Parameterized query
        parameterization_form_cls = make_parameterization_form(query_str)
        if parameterization_form_cls:
          parameterization_form = parameterization_form_cls(request.REQUEST, prefix="parameterization")

          if parameterization_form.is_valid():
            real_query = substitute_variables(query_str, parameterization_form.cleaned_data)
            query = HQLdesign(query_form, query_type=query_type)
            query._data_dict['query']['query'] = real_query

            try:
              if explain:
                return explain_directly(request, query, design, query_server)
              else:
                return execute_directly(request, query, design, query_server)

            except Exception, ex:
              db = dbms.get(request.user, query_server)
              error_message, log = expand_exception(ex, db)
              response['message'] = error_message
              return HttpResponse(json.dumps(response), mimetype="application/json")
          else:
            response['errors'] = parameterization_form.errors
            return HttpResponse(json.dumps(response), mimetype="application/json")

      # non-parameterized query
      query = HQLdesign(query_form, query_type=query_type)
      if request.GET.get('explain', 'false').lower() == 'true':
        return explain_directly(request, query, design, query_server)
      else:
        return execute_directly(request, query, design, query_server)
    else:
      response['message'] = _('There was an error with your query.')
      response['errors'] = query_form.query.errors
  except RuntimeError, e:
    response['message']= str(e)

  return HttpResponse(json.dumps(response), mimetype="application/json")


@error_handler
def save_query(request, query_id=None):
  response = {'status': -1, 'message': ''}

  if request.method != 'POST':
    response['message'] = _('A POST request is required.')

  app_name = get_app_name(request)
  query_type = beeswax.models.SavedQuery.TYPES_MAPPING[app_name]
  design = safe_get_design(request, query_type, query_id)

  try:
    query_form = get_query_form(request)

    if query_form.is_valid():
      design = save_design(request, query_form, query_type, design, True)
      response['design_id'] = design.id
      response['status'] = 0
    else:
      response['errors'] = query_form.errors
  except RuntimeError, e:
    response['message'] = str(e)

  return HttpResponse(json.dumps(response), mimetype="application/json")


@error_handler
def fetch_saved_query(request, query_id):
  response = {'status': 0, 'message': ''}

  if request.method != 'GET':
    response['message'] = _('A GET request is required.')

  app_name = get_app_name(request)
  query_type = beeswax.models.SavedQuery.TYPES_MAPPING[app_name]
  design = safe_get_design(request, query_type, query_id)

  response['design'] = design_to_dict(design)
  return HttpResponse(json.dumps(response), mimetype="application/json")


@error_handler
def cancel_query(request, query_id):
  response = {'status': -1, 'message': ''}

  if request.method != 'POST':
    response['message'] = _('A POST request is required.')
  else:
    try:
      query_history = authorized_get_history(request, query_id, must_exist=True)
      db = dbms.get(request.user, query_history.get_query_server_config())
      db.cancel_operation(query_history.get_handle())
      _get_query_handle_and_state(query_history)
      response['status'] = 0
    except Exception, e:
      response['message'] = unicode(e)

  return HttpResponse(json.dumps(response), mimetype="application/json")


def design_to_dict(design):
  hql_design = HQLdesign.loads(design.data)
  return {
    'id': design.id,
    'query': hql_design.hql_query,
    'name': design.name,
    'desc': design.desc,
    'database': hql_design.query.get('database', None),
    'settings': hql_design.settings,
    'file_resources': hql_design.file_resources,
    'functions': hql_design.functions,
    'is_parameterized': hql_design.query.get('is_parameterized', True),
    'email_notify': hql_design.query.get('email_notify', True)
  }


def get_query_form(request):
  # Get database choices
  query_server = dbms.get_query_server_config(get_app_name(request))
  db = dbms.get(request.user, query_server)
  databases = [(database, database) for database in db.get_databases()]

  if not databases:
    raise RuntimeError(_("No databases are available. Permissions could be missing."))

  query_form = QueryForm()
  query_form.bind(request.POST)
  query_form.query.fields['database'].choices = databases # Could not do it in the form

  return query_form
