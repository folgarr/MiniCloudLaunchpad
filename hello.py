from flask import Flask, render_template, request, Response, session, flash, \
    url_for, redirect, stream_with_context
import boto.cloudformation
import urllib2
import boto.ec2
import boto
import uuid
import time
import os

default_region = 'us-east-1'
cf_template_url = 'https://bitnami.com/cloudformation/wordpress.template'
app = Flask(__name__)
app.secret_key = "\xd4\xc0Pm\\\x11\x84f\x1c\x1a\xe50\xe6j-*\xf7[\x1f]\x1a\x8d\x97P"
session_cache = {}

class CredentialsError(Exception):
  pass

@app.route('/')
def hello():
  if not session.get('session_cache_id') or not session_cache.get(session['session_cache_id']):
    session['session_cache_id'] = uuid.uuid4().hex
    _cache_session(session)
  return render_template('index.html')

@app.route('/create_stack', methods=['POST'])
def create_stack():
  """
  Launches the creation of a WordPress stack using AWS CloudFormation.
  CloudFormation uses a bitnami template to set-up and configure all stack resources.
  """
  try:
    acc_key = request.form.get('acc_key')
    secret_key = request.form.get('sec_acc_key')

    session['acc_key'] = acc_key
    session['secret_key'] = secret_key
    _cache_session(session)

    credentials = {'access_key': acc_key, 'secret_access_key': secret_key}
    _cf = _create_connection('cloudformation', **credentials)
    _ec2 = _create_connection('ec2', **credentials)

    # obtain the template that cloudformation uses to create the stack
    template = ""
    for line in urllib2.urlopen(cf_template_url):
      template += line

    # create and store a 'default' keypair for later ssh usage
    keypairs = _ec2.get_all_key_pairs()
    if 'default' not in [k.name for k in keypairs]:
      key = _ec2.create_key_pair('default')
      key_dir = os.path.expanduser("~/.ssh")
      key_dir = os.path.expandvars(key_dir)
      if not os.path.isdir(key_dir):
        os.mkdir(key_dir, 0700)
      # remove old default.pem if its exist
      full_file_path = key_dir + '/default.pem'
      if os.path.isfile(full_file_path):
        os.remove(full_file_path)
      key.save(key_dir)

    # launch the CloudFormation stack creation process
    _cf.create_stack('BitnamiWordpressStack', template_body=template)

    # generator function to yield stack creation status messages to client
    @stream_with_context
    def progress_updates_generator():
      stack = _cf.describe_stacks('BitnamiWordpressStack')[0]
      timeout = time.time() + 60*5
      all_current_events, last_update_events = [], []
      while not stack.outputs:
        # grab all events and send only the newest to the browser
        all_current_events = _stack_events_list(stack)
        new_events = [e for e in all_current_events if e not in last_update_events]
        last_update_events = all_current_events
        yield new_events
        time.sleep(0.2)
        # grab the stack in its new updated state
        stack = _cf.describe_stacks('BitnamiWordpressStack')[0]
        if time.time() > timeout:
          yield ["Error: time limit of 5 minutes exceed in creating the stack."]
          break
      if stack.outputs:
        wordpress_url = stack.outputs[0].value
        yield [wordpress_url]
    return Response(stream_template('show_progress.html', event_updates=progress_updates_generator()))
  except Exception as e:
    return str(e)

@app.route('/shutdown_vm', methods=['POST'])
def shutdown_vm():
  """
  Stops the EC2 instance associated with the WordPress stack
  that user last launched.
  """
  try:
    session_cache_id = session['session_cache_id']
    cached_session = session_cache[session_cache_id]
    acc_key = cached_session['acc_key']
    secret_key = cached_session['secret_key']
    credentials = {'access_key': acc_key, 'secret_access_key': secret_key}
    _cf = _create_connection('cloudformation', **credentials)
    _ec2 = _create_connection('ec2', **credentials)
    stack = _cf.describe_stacks('BitnamiWordpressStack')[0]
    for resource in stack.list_resources():
      if 'AWS::EC2::Instance' in resource.resource_type:
        _ec2.stop_instances(instance_ids=[resource.physical_resource_id])
        flash('You were able to send the stop signal to the VM.')
        return redirect(url_for('hello'))
    flash("Unable to find and shut down the VM.")
    return redirect(url_for('hello'))
  except Exception as e:
    return str(e)

def _stack_events_list(stack):
  """
  Returns a list of strings, where each string shows the resource type
  and resource status of each resource associated with the stack.
  """
  events = stack.describe_events()
  result = []
  for e in events:
    event_parts = str(e).split()
    result.append(event_parts[1] + ' ' + event_parts[3])
  return result

def _create_connection(service_name, **options):
  region = options.get('region', default_region)
  acc_key = options.get('access_key')
  secret_key = options.get('secret_access_key')
  if service_name not in ('ec2', 'cloudformation'):
    raise ValueError('Invalid service name: {}. Unable to create a connection.'.format(service_name))
  if not acc_key:
    raise CredentialsError('Missing required value: access key.')
  if not secret_key:
    raise CredentialsError('Missing required value: secret access key.')

  if service_name == 'cloudformation':
    conn = boto.cloudformation.connect_to_region(region, aws_access_key_id=acc_key, aws_secret_access_key=secret_key)
  if service_name == 'ec2':
    conn = boto.ec2.connect_to_region(region, aws_access_key_id=acc_key, aws_secret_access_key=secret_key)

  if not conn:
    raise CredentialsError('Unable to log-in with given AWS credentials. \
                           Please check validity and permissions of credentials.')
  return conn

def _cache_session(s):
  """
  Stores the key-value pairs from the current session to prevent
  losing it across requests. For details as to why we dont wholly rely on
  the default Flask session object functionality see the following:
    https://github.com/mitsuhiko/flask/issues/1348
  (tldr: sessions arent persisted in correct manner when streaming templates)
  """
  session_cache_id = s['session_cache_id']
  session_cache[session_cache_id] = dict(s.items())

def stream_template(template_name, **context):
  """
  Renders the template on the client side piece by piece, where
  each piece corresponds to the output of a generator.
  """
  app.update_template_context(context)
  t = app.jinja_env.get_template(template_name)
  rv = t.stream(context)
  rv.enable_buffering(5)
  return rv

if __name__ == "__main__":
  app.run()

