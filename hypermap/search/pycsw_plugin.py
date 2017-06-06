# -*- coding: iso-8859-15 -*-
# =================================================================
#
# Authors: Tom Kralidis <tomkralidis@gmail.com>
#
# Copyright (c) 2016 Tom Kralidis
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# =================================================================

import inspect
import json
import logging

from django.conf import settings

from pycsw.core import util
import requests
from hypermap.aggregator.models import Catalog, Layer, Service, Endpoint, SpatialReferenceSystem
from hypermap.aggregator.utils import create_layer_from_metadata_xml
from hypermap.aggregator.tasks import index_layer

LOGGER = logging.getLogger(__name__)

HYPERMAP_SERVICE_TYPES = {
    # 'HHypermap enum': 'CSW enum'
    'http://www.opengis.net/cat/csw/2.0.2': 'OGC:CSW',
    'http://www.opengis.net/wms': 'OGC:WMS',
    'http://www.opengis.net/wmts/1.0': 'OGC:WMTS',
    'https://wiki.osgeo.org/wiki/TMS': 'OSGeo:TMS',
    'urn:x-esri:serviceType:ArcGIS:MapServer': 'ESRI:ArcGIS:MapServer',
    'urn:x-esri:serviceType:ArcGIS:ImageServer': 'ESRI:ArcGIS:ImageServer'
}


def get_service(raw_xml):
    """
    Set a service object based on the XML metadata
       <dct:references scheme="OGC:WMS">http://ngamaps.geointapps.org/arcgis
       /services/RIO/Rio_Foundation_Transportation/MapServer/WMSServer
       </dct:references>
    :param instance:
    :return: Layer
    """
    from pycsw.core.etree import etree

    parsed = etree.fromstring(raw_xml, etree.XMLParser(resolve_entities=False))

    # <dc:format>OGC:WMS</dc:format>
    source_tag = parsed.find("{http://purl.org/dc/elements/1.1/}source")
    # <dc:source>
    #    http://ngamaps.geointapps.org/arcgis/services/RIO/Rio_Foundation_Transportation/MapServer/WMSServer
    # </dc:source>
    format_tag = parsed.find("{http://purl.org/dc/elements/1.1/}format")

    service_url = None
    service_type = None

    if hasattr(source_tag, 'text'):
        service_url = source_tag.text

    if hasattr(format_tag, 'text'):
        service_type = format_tag.text

    if hasattr(format_tag, 'text'):
        service_type = format_tag.text

    service, created = Service.objects.get_or_create(url=service_url,
                                                     is_monitored=False,
                                                     type=service_type)
    # TODO: dont hardcode SRS, get them from the parsed XML.
    srs, created = SpatialReferenceSystem.objects.get_or_create(code="EPSG:4326")
    service.srs.add(srs)

    return service


class HHypermapRepository(object):
    ''' Class to interact with underlying repository '''
    def __init__(self, context, repo_filter=None):
        ''' Initialize repository '''

        self.context = context
        self.filter = repo_filter
        self.fts = False
        self.label = 'HHypermap'
        self.local_ingest = True
#        self.url = settings.SEARCH_URL
        self.url = 'http://elasticsearch:9200/hypermap'

        self.dbtype = settings.DATABASES['default']['ENGINE'].split('.')[-1]

        # HHypermap PostgreSQL installs are PostGIS enabled
        if self.dbtype == 'postgis':
            self.dbtype = 'postgresql+postgis+wkt'

        # generate core queryables db and obj bindings
        self.queryables = {}

        for tname in self.context.model['typenames']:
            for qname in self.context.model['typenames'][tname]['queryables']:
                self.queryables[qname] = {}
                items = self.context.model['typenames'][tname]['queryables'][qname].items()

                for qkey, qvalue in items:
                    self.queryables[qname][qkey] = qvalue

        # flatten all queryables
        # TODO smarter way of doing this
        self.queryables['_all'] = {}
        for qbl in self.queryables:
            self.queryables['_all'].update(self.queryables[qbl])
        self.queryables['_all'].update(self.context.md_core_model['mappings'])

        if 'Harvest' in self.context.model['operations'] and 'Transaction' in self.context.model['operations']:
            self.context.model['operations']['Harvest']['parameters']['ResourceType']['values'] = HYPERMAP_SERVICE_TYPES.keys()  # noqa
            self.context.model['operations']['Transaction']['parameters']['TransactionSchemas']['values'] = HYPERMAP_SERVICE_TYPES.keys()  # noqa

    def dataset(self):
        ''' Stub to mock a pycsw dataset object for Transactions'''
        return type('Service', (object,), {})

    def query_domain(self, domain, typenames, domainquerytype='list', count=False):
        ''' Query by property domain values '''

        # objects = self._get_repo_filter(Layer.objects)

        if domainquerytype == 'range':
            url = '{}/_field_stats'.format(self.url)

            params = {
                'fields': domain
            }

            response = requests.get(url, params=params).json()
            LOGGER.info(response)
            LOGGER.info(response)

            min_ = response['indices']['_all']['fields'][domain]['min_value']
            max_ = response['indices']['_all']['fields'][domain]['max_value']

            return [(min_, max_)]

        else:
            url = '{}/_search'.format(self.url)

            data = {
                'size': 0,
                'aggs': {
                    'domain': {
                        'terms': {
                            'field': domain
                        }
                    }
                }
            }

            LOGGER.info('data %s', data)
            response = requests.post(url, json=data).json()

            out = []
            for b in response['aggregations']['domain']['buckets']:
                out.append((b['key'], b['doc_count']))

            return out

    def query_insert(self, direction='max'):
        ''' Query to get latest (default) or earliest update to repository '''

        url = '{}/_search'.format(self.url)

        field = 'layer_date'

        if direction == 'min':
            direction = 'asc'
        else:
            direction = 'desc'

        params = {
            'sort': '{}:{}'.format(field, direction),
            '_source': field,
            'size': 1,
        }

        response = requests.get(url, params=params).json()
        latest_insert = response['hits']['hits'][0]['_source'][field]
        return latest_insert

    def query_source(self, source):
        ''' Query by source '''
        return self._get_repo_filter(Layer.objects).filter(url=source)

    def query_ids(self, ids):
        ''' Query by identifiers '''

        return self.query({}, ids=ids)[1]

    def query(self, constraint, sortby=None, typenames=None, maxrecords=10, startposition=0, ids=[]):
        ''' Query records from underlying repository '''

        params = {}
        results = []
        url = '{}/_search'.format(self.url)

        LOGGER.info('Constraint: %s', constraint)
#        LOGGER.info('RAW %r', constraint)

        if ids:
            id_ = ids[0]
            if 'service:' in id_:
                id_ = 'service_id'
            LOGGER.info('ids based search')
            params['q'] = 'id:{}'.format(id_)
        else:
            params['size'] = maxrecords
            params['from'] = startposition 

            if 'where' in constraint:
                params['q'] = constraint['where'] % tuple(constraint['values'])
            else:  # empty query
                LOGGER.info('No filter specified')

            if sortby is not None:
                sortvalue = '{}:{}'.format(sortby['propertyname'], sortby['order'].lower())
                params['sort'] = sortvalue

        LOGGER.info('QUERY: %r', params)
        response = requests.get(url, params=params).json()
        LOGGER.info('Elasticsearch response %s', response)

        es_response = response['hits']['hits']
        count = response['hits']['total']

        for hit in es_response:
            result = hit['_source']
            result['id_string'] = result['id']
            result['wkt_geometry'] = result['bbox']
            result['type'] = result['service_type']
            result['csw_type'] = 'dataset'
            result['download_links'] = _make_download_links(result)
            results.append(type('', (object,), result)())

        return [str(count), results]

    def insert(self, resourcetype, source, insert_date=None):
        ''' Insert a record into the repository '''

        caller = inspect.stack()[1][3]

        if caller == 'transaction':  # insert of Layer
            hhclass = 'Layer'
            source = resourcetype
            resourcetype = resourcetype.csw_schema
        else:  # insert of service
            hhclass = 'Service'
            if resourcetype not in HYPERMAP_SERVICE_TYPES.keys():
                raise RuntimeError('Unsupported Service Type')

        return self._insert_or_update(resourcetype, source, mode='insert', hhclass=hhclass)

    def _insert_or_update(self, resourcetype, source, mode='insert', hhclass='Service'):
        ''' Insert or update a record in the repository '''

        keywords = []

        if self.filter is not None:
            catalog = Catalog.objects.get(id=int(self.filter.split()[-1]))

        try:
            if hhclass == 'Layer':
                # TODO: better way of figuring out duplicates
                match = Layer.objects.filter(name=source.name,
                                             title=source.title,
                                             abstract=source.abstract,
                                             is_monitored=False)
                matches = match.all()
                if matches:
                    if mode == 'insert':
                        raise RuntimeError('HHypermap error: Layer %d \'%s\' already exists' % (
                                           matches[0].id, source.title))
                    elif mode == 'update':
                        match.update(
                            name=source.name,
                            title=source.title,
                            abstract=source.abstract,
                            is_monitored=False,
                            xml=source.xml,
                            wkt_geometry=source.wkt_geometry,
                            anytext=util.get_anytext([source.title, source.abstract, source.keywords_csv])
                        )

                service = get_service(source.xml)
                res, keywords = create_layer_from_metadata_xml(resourcetype, source.xml,
                                                               monitor=False, service=service,
                                                               catalog=catalog)

                res.save()

                LOGGER.debug('Indexing layer with id %s on search engine' % res.id)
                index_layer(res, use_cache=True)

            else:
                if resourcetype == 'http://www.opengis.net/cat/csw/2.0.2':
                    res = Endpoint(url=source, catalog=catalog)
                else:
                    res = Service(type=HYPERMAP_SERVICE_TYPES[resourcetype], url=source, catalog=catalog)

                res.save()

            if keywords:
                for kw in keywords:
                    res.keywords.add(kw)
        except Exception as err:
            raise RuntimeError('HHypermap error: %s' % err)

        # return a list of ids that were inserted or updated
        ids = []

        if hhclass == 'Layer':
            ids.append({'identifier': res.uuid, 'title': res.title})
        else:
            if resourcetype == 'http://www.opengis.net/cat/csw/2.0.2':
                for res in Endpoint.objects.filter(url=source).all():
                    ids.append({'identifier': res.uuid, 'title': res.url})
            else:
                for res in Service.objects.filter(url=source).all():
                    ids.append({'identifier': res.uuid, 'title': res.title})

        return ids

    def delete(self, constraint):
        ''' Delete a record from the repository '''

        results = self._get_repo_filter(Service.objects).extra(where=[constraint['where']],
                                                               params=constraint['values']).all()
        deleted = len(results)
        results.delete()
        return deleted

    def _get_repo_filter(self, query):
        ''' Apply repository wide side filter / mask query '''
        if self.filter is not None:
            return query.extra(where=[self.filter])
        return query


def _make_download_links(result):
    """create pycsw download links column from HH result"""

    # format n,d,p,u[^]

    service_link = [
        result['name'],
        result['name'],
        result['service_type'],
        result['url']
    ]

    www_link = [
        result['name'],
        result['name'],
        'WWW:LINK',
        '{}{}'.format(settings.SITE_URL, result['location']['layer_info'])
    ]

    return '^'.join([','.join(service_link), ','.join(www_link)])
