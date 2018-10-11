"""
WSGI config for squad project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/1.9/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "squad.settings")

application = get_wsgi_application()


# TODO it must be moved to openshift's template, but for a PoC is good enough
if not os.path.exists('/tmp/nasty.hack'):
    os.system('touch /tmp/nasty.hack')
    os.system('nohup celery -A squad worker --concurrency=16 &')
    os.system('nohup celery -A squad beat &')
    os.system('nohup ./manage.py listen &')
