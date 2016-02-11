from __future__ import absolute_import

from celery import shared_task


@shared_task(bind=True)
def check_all_services(self):
    from aggregator.models import Service
    service_to_processes = Service.objects.filter(active=True)
    total = service_to_processes.count()
    count = 0
    for service in service_to_processes:
        # update state
        if not self.request.called_directly:
            self.update_state(state='PROGRESS',
                meta={'current': count, 'total': total})
        service.update_layers()
        service.check()
        for layer in service.layer_set.all():
            layer.check()
        count = count + 1


@shared_task(bind=True)
def check_service(self, service):
    layer_to_process = service.layer_set.all()
    total = layer_to_process.count()
    def status_update(count):
        if not self.request.called_directly:
            self.update_state(state='PROGRESS',
                meta={'current': count, 'total': total})

    print 'Checking service %s' % service.title
    status_update(0)
    service.update_layers()
    status_update(1)
    service.check()
    status_update(2)
    count = 3 # we count 1 for update_layers and 1 for service check for simplicity
    for layer in layer_to_process:
        # update state
        status_update(count)
        layer.check()
        count = count + 1


@shared_task(name="check_specific_layer")
def check_layer(layer):
    print 'Checking layer %s' % layer.name
    layer.check()


@shared_task(name="update_endpoints")
def update_endpoints(endpoint_list):
    from aggregator.utils import create_services_from_endpoint, get_sanitized_endpoint
    for endpoint in endpoint_list.endpoint_set.all():
        if not endpoint.processed:
            print endpoint.url
            sanitized_url = get_sanitized_endpoint(endpoint.url)
            imported, message = create_services_from_endpoint(sanitized_url)
            endpoint.imported = imported
            endpoint.message = message
            endpoint.processed = True
            endpoint.save()
        else:
            print 'This enpoint was already processed'
    return True
