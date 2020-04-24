#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""diskover - Elasticsearch file system crawler
diskover is a file system crawler that index's
your file metadata into Elasticsearch.
See README.md or https://github.com/shirosaidev/diskover
for more information.

Copyright (C) Chris Park 2017-2019
diskover is released under the Apache 2.0 license. See
LICENSE for the full license text.
"""

from diskover import index_bulk_add, config, es, progress_bar, redis_conn, worker_bots_busy, ab_start, adaptive_batch
from diskover_bot_module import dupes_process_hashkeys
from rq import SimpleWorker
import base64
import hashlib
import os
import time
import warnings
try:
    from Queue import Queue as pyQueue
except ImportError:
    from queue import Queue as pyQueue
from threading import Thread
from multiprocessing import cpu_count


def index_dupes(hashgroup, cliargs):
    """This is the ES dupe_md5 tag update function.
    It updates a file's dupe_md5 field to be md5sum of file
    if it's marked as a duplicate.
    """

    file_id_list = []
    # bulk update data in Elasticsearch index
    for f in hashgroup['files']:
        d = {
            '_op_type': 'update',
            '_index': cliargs['index'],
            '_type': 'file',
            '_id': f['id'],
            'doc': {'dupe_md5': f['md5']}
        }
        file_id_list.append(d)
    if len(file_id_list) > 0:
        index_bulk_add(es, file_id_list, config, cliargs)


def start_file_threads():
    for i in range(config['dupes_threads']):
        thread = Thread(target=md5_hasher)
        thread.daemon = True
        thread.start()


def md5_hasher():
    while True:
        item = file_in_thread_q.get()
        filename, atime, mtime, cliargs = item
        # get md5 sum, don't load whole file into memory,
        # load in n bytes at a time (read_size blocksize)
        try:
            read_size = config['md5_readsize']
            hasher = hashlib.md5()
            with open(filename, 'rb') as f:
                buf = f.read(read_size)
                while len(buf) > 0:
                    hasher.update(buf)
                    buf = f.read(read_size)
            md5 = hasher.hexdigest()

            # restore times (atime/mtime)
            if config['dupes_restoretimes'] == "true":
                atime_unix = time.mktime(time.strptime(atime, '%Y-%m-%dT%H:%M:%S'))
                mtime_unix = time.mktime(time.strptime(mtime, '%Y-%m-%dT%H:%M:%S'))
                try:
                    os.utime(filename, (atime_unix, mtime_unix))
                except (OSError, IOError) as e:
                    warnings.warn("OS/IO Exception caused by: %s" % e)
                    pass
                except Exception as e:
                    warnings.warn("Exception caused by: %s" % e)
                    pass
        except (OSError, IOError) as e:
            warnings.warn("OS/IO Exception caused by: %s" % e)
            file_in_thread_q.task_done()
            continue
        except Exception as e:
            warnings.warn("Exception caused by: %s" % e)
            file_in_thread_q.task_done()
            continue
        file_out_thread_q.put((filename, md5))
        file_in_thread_q.task_done()


def verify_dupes(filehash_filelist, cliargs):
    """This is the verify dupes function.
    It processes files in filehash_filelist to verify if they are duplicate.
    The first few bytes at beginning and end of files are compared and if same, 
    a md5 check is run on the files.
    """

    # number of bytes to check at start and end of file
    read_bytes = config['dupes_checkbytes']

    # min bytes to read of file size less than above
    min_read_bytes = 1

    dups = {}

    # Add first and last few bytes for each file to dups dictionary

    file_count = 0
    for file in filehash_filelist['files']:
        try:
            f = open(file['filename'], 'rb')
        except (OSError, IOError) as e:
            warnings.warn("OS/IO Exception caused by: %s" % e)
            continue
        except Exception as e:
            warnings.warn("Exception caused by: %s" % e)
            continue
        # check if files is only 1 byte
        try:
            bytes_f = base64.b64encode(f.read(read_bytes))
        except (IOError, OSError):
            pass
            try:
                bytes_f = base64.b64encode(f.read(min_read_bytes))
            except Exception as e:
                warnings.warn("Exception caused by: %s" % e)
                continue
        try:
            f.seek(-read_bytes, os.SEEK_END)
            bytes_l = base64.b64encode(f.read(read_bytes))
        except (IOError, OSError):
            pass
            try:
                f.seek(-min_read_bytes, os.SEEK_END)
                bytes_l = base64.b64encode(f.read(min_read_bytes))
            except Exception as e:
                warnings.warn("Exception caused by: %s" % e)
                continue
        f.close()
        # restore times (atime/mtime)
        if config['dupes_restoretimes'] == "true":
            atime_unix = time.mktime(time.strptime(file['atime'], '%Y-%m-%dT%H:%M:%S'))
            mtime_unix = time.mktime(time.strptime(file['mtime'], '%Y-%m-%dT%H:%M:%S'))
            try:
                os.utime(file['filename'], (atime_unix, mtime_unix))
            except (OSError, IOError) as e:
                warnings.warn("OS/IO Exception caused by: %s" % e)
                pass
            except Exception as e:
                warnings.warn("Exception caused by: %s" % e)
                pass

        # create hash of bytes
        bytestring = str(bytes_f) + str(bytes_l)
        bytehash = hashlib.md5(bytestring.encode('utf-8')).hexdigest()

        # Add or append the file to dups dict
        if bytehash in dups:
            dups[bytehash].append((file['filename'], file['atime'], file['mtime']))
        else:
            dups[bytehash] = [(file['filename'], file['atime'], file['mtime'])]

        file_count += 1

    if file_count == 0:
        return None

    # remove any bytehash key that only has 1 item (no duplicate)
    for key in [key for key in dups if len(dups[key]) < 2]: del dups[key]

    if not dups:
        return None

    # run md5 sum check if bytes were same

    dups_md5 = {}

    # do md5 check on files with same byte hashes
    for key, value in dups.items():
        for file in value:
            filename, atime, mtime = file
            # add file into thread queue
            file_in_thread_q.put((filename, atime, mtime, cliargs))

        # wait for threads to finish
        file_in_thread_q.join()

        # get all files from queue
        while True:
            item = file_out_thread_q.get()
            filename, md5 = item

            # Add or append the file to dups_md5 dict
            if md5 in dups_md5:
                dups_md5[md5].append(filename)
            else:
                dups_md5[md5] = [filename]
            
            file_out_thread_q.task_done()

            if file_out_thread_q.qsize() == 0:
                break

    if not dups_md5:
        return None

    # update md5 key in filehash_filelist for each file in dups_md5
    for key, value in dups_md5.items():
        if len(value) >= 2:
            for i in range(len(filehash_filelist['files'])):
                if filehash_filelist['files'][i]['filename'] in value:
                    filehash_filelist['files'][i]['md5'] = key
    
    return filehash_filelist


def dupes_finder(es, q, cliargs, logger):
    """This is the duplicate file finder function.
    It searches Elasticsearch for files that have the same filehashes
    and adds file hash groups to Queue.
    """

    logger.info('Searching %s for all dupe filehashes...', cliargs['index'])

    # first get all the filehashes with files that have a hardlinks count of 1
    if cliargs['inchardlinks']:
        data = {
                "size": 0,
                "_source": ['filename', 'filehash', 'path_parent', 'last_modified', 'last_access'],
                "query": {
                    "bool": {
                        "must": {
                            "range": {
                                "filesize": {
                                    "lte": config['dupes_maxsize'],
                                    "gte": cliargs['minsize']
                                }
                            }
                        }
                    }
                }
            }
    else:
        data = {
            "size": 0,
            "_source": ['filename', 'filehash', 'path_parent', 'last_modified', 'last_access'],
            "query": {
                "bool": {
                    "must": { 
                        "term": {"hardlinks": 1} 
                    },
                    "filter": {
                        "range": {
                            "filesize": {
                                "lte": config['dupes_maxsize'],
                                "gte": cliargs['minsize']
                            }
                        }
                    }
                }
            }
        }

    # refresh index
    es.indices.refresh(index=cliargs['index'])
    # search es and start scroll
    res = es.search(index=cliargs['index'], doc_type='file', scroll='1m', size=config['es_scrollsize'],
                    body=data, request_timeout=config['es_timeout'])

    filehashes = {}
    while res['hits']['hits'] and len(res['hits']['hits']) > 0:
        for hit in res['hits']['hits']:
            filehash = hit['_source']['filehash']
            filepath = os.path.join(hit['_source']['path_parent'], hit['_source']['filename'])
            if filehash in filehashes:
                filehashes[filehash].append(
                    {'id': hit['_id'],
                    'filename': filepath,
                    'atime': hit['_source']['last_access'],
                    'mtime': hit['_source']['last_modified'], 'md5': ''})
            else:
                filehashes[filehash] = [
                    {'id': hit['_id'],
                    'filename': filepath,
                    'atime': hit['_source']['last_access'],
                    'mtime': hit['_source']['last_modified'], 'md5': ''}
                ]
            
        # use es scroll api
        res = es.scroll(scroll_id=res['_scroll_id'], scroll='1m',
                        request_timeout=config['es_timeout'])

    possibledupescount = 0
    for key, value in list(filehashes.items()):
        filehash_filecount = len(value)
        if filehash_filecount < 2:
            del filehashes[key]
        else:
            possibledupescount += filehash_filecount

    logger.info('Found %s possible dupe files', possibledupescount)
    if possibledupescount == 0:
        return
        
    logger.info('Starting to enqueue dupe file hashes...')

    if cliargs['adaptivebatch']:
        batchsize = ab_start
    else:
        batchsize = cliargs['batchsize']
    if cliargs['verbose'] or cliargs['debug']:
        logger.info('Batch size: %s' % batchsize)

    n = 0
    hashgroups = []
    for key, value in filehashes.items():
        if cliargs['verbose'] or cliargs['debug']:
            logger.info('filehash: %s, filecount: %s' %(key, len(value)))
        hashgroups.append({'filehash': key, 'files': value})
        n += 1
        if n >= batchsize:
            # send to rq for bots to process hashgroups list
            q.enqueue(dupes_process_hashkeys, args=(hashgroups, cliargs,), result_ttl=config['redis_ttl'])
            if cliargs['debug'] or cliargs['verbose']:
                logger.info("enqueued batchsize: %s (batchsize: %s)" % (n, batchsize))
            del hashgroups[:]
            n = 0
            if cliargs['adaptivebatch']:
                batchsize = adaptive_batch(q, cliargs, batchsize)
                if cliargs['debug'] or cliargs['verbose']:
                    logger.info("batchsize set to: %s" % batchsize)

    # enqueue dir calc job for any remaining in dirlist
    if n > 0:
        q.enqueue(dupes_process_hashkeys, args=(hashgroups, cliargs,), result_ttl=config['redis_ttl'])

    logger.info('%s possible dupe file hashes have been enqueued, worker bots processing dupes...' % possibledupescount)

    if not cliargs['quiet'] and not cliargs['debug'] and not cliargs['verbose']:
        bar = progress_bar('Checking')
        bar.start()
    else:
        bar = None

    # update progress bar until bots are idle and queue is empty
    while worker_bots_busy([q]):
        if bar:
            q_len = len(q)
            try:
                bar.update(q_len)
            except (ZeroDivisionError, ValueError):
                bar.update(0)
        time.sleep(1)

    if bar:
        bar.finish()


# set up python Queue for threaded file md5 checking
file_in_thread_q = pyQueue()
file_out_thread_q = pyQueue()
start_file_threads()