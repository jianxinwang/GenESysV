import argparse
import os
import json
import re
from collections import deque
from pprint import pprint
from tqdm import tqdm
from datetime import datetime
import sys
import statistics
import time
import elasticsearch
from elasticsearch import helpers
import re
from collections import Counter
import asyncio
import functools
import requests
from es_celery.tasks import post_data, update_refresh_interval
import hashlib
import math
import tempfile


GLOBAL_NO_VARIANTS_PROCESSED = 0
GLOBAL_NO_VARIANTS_CREATED = 0
GLOBAL_NO_VARIANTS_UPDATED = 0

class VCFException(Exception):
    """Raise for my specific kind of exception"""
    def __init__(self, message, *args):
        self.message = message # without this you may get DeprecationWarning
        # Special attribute you desire with your Error,
        # perhaps the value that caused the error?:
        # allow users initialize misc. arguments as any other builtin Error
        super(VCFException, self).__init__(message, *args)


def get_es_id(CHROM, POS, REF, ALT, index_name, type_name):
    es_id = '%s%s%s%s%s%s' %(CHROM, POS, REF, ALT, index_name, type_name)
    es_id = es_id.encode('utf-8')
    es_id = hashlib.sha224(es_id).hexdigest()

    return es_id

def prune_array(key, input_array):
    key_count = Counter([ele[key] for ele in input_array])

    output_array = []
    for ele in input_array:
        tmp_key = ele[key]
        if key_count[tmp_key] == 1:
            output_array.append(ele)
        elif key_count[tmp_key] > 1:
            if len(ele) > 1:
                output_array.append(ele)

    return output_array

def estimate_no_variants_in_file(filename, no_lines_for_estimating):
    no_lines = 0
    size_list = deque()

    with open(filename, 'r') as fp:
        for line in fp:
            if line.startswith('#'):
                continue

            if no_lines_for_estimating < no_lines:
                break

            size_list.appendleft(sys.getsizeof(line))

            no_lines += 1

    filesize = os.path.getsize(filename)

    no_variants = int(filesize/statistics.median(size_list))

    return no_variants

def CHROM_parser(input_string):
    return input_string.lower().replace('chr','').strip()

def gwasCatalog_parser(input_string):
    return input_string.replace('|', ' ')

def clinvar_parser(input_dict):
    output = []
    size = len(input_dict['CLNDBN'].split('|'))
    CLINSIG_split = input_dict['CLINSIG'].split('|')
    CLNDBN_split = input_dict['CLNDBN'].split('|')
    CLNACC_split = input_dict['CLNACC'].split('|')
    CLNDSDB_split = input_dict['CLNDSDB'].split('|')
    CLNDSDBID_split = input_dict['CLNDSDBID'].split('|')

    for i in range(size):
        CLINSIG = CLINSIG_split[i]
        CLNDBN = CLNDBN_split[i]
        CLNACC = CLNACC_split[i]
        CLNDSDB = CLNDSDB_split[i]
        CLNDSDBID = CLNDSDBID_split[i]
        output.append({'clinvar_CLINSIG': CLINSIG,
                        'clinvar_CLNDBN': CLNDBN,
                        'clinvar_CLNACC': CLNACC,
                        'clinvar_CLNDSDB': CLNDSDB,
                        'clinvar_CLNDSDBID': CLNDSDBID
                        })

    return output

def GTEx_V6_tissue_parser(input_string):
    return input_string.replace('|', ' ')

def GTEx_V6_gene_parser(input_string):
    return input_string.replace('|', ' ')

def Gene_refGene_parser(relevant_info_fields):

    pattern = r'^dist=[a-zA-Z0-9]+;dist=[a-zA-Z0-9]+$'

    Gene_refGene = relevant_info_fields['Gene.refGene']
    symbol = ' '.join(re.split('[;,]', Gene_refGene))

    tmp_content_array = []

    if relevant_info_fields.get('GeneDetail.refGene'):

        GeneDetail_refGene = convert_escaped_chars(relevant_info_fields.get('GeneDetail.refGene'))

        if re.match(pattern, GeneDetail_refGene):
            tmp_content = {}
            tmp_content['refGene_symbol'] = symbol
            tmp_content['refGene_distance_to_gene'] = GeneDetail_refGene
            tmp_content_array.append(tmp_content)

        elif ':' in GeneDetail_refGene:
            for record in GeneDetail_refGene.split(','):
                # print(record)
                tmp_content = {}
                for ele in record.split(':'):
                    if ele.startswith('N'):
                        tmp_content['refGene_refgene_id'] = ele
                    elif ele.startswith('exon'):
                        tmp_content['refGene_location'] = ele
                    elif ele.startswith('c.'):
                        tmp_content['refGene_cDNA_change'] = ele

                tmp_content['refGene_symbol'] = symbol
                # print(tmp_content)
                tmp_content_array.append(tmp_content)

    else:
        tmp_content = {}
        tmp_content['refGene_symbol'] = symbol
        tmp_content_array.append(tmp_content)

    return tmp_content_array


def Gene_ensGene_parser(relevant_info_fields):

    pattern = r'^dist=[a-zA-Z0-9]+;dist=[a-zA-Z0-9]+$' # pattern to detect dist
    Gene_ensGene = relevant_info_fields["Gene.ensGene"]

    gene_id = ' '.join(re.split('[;,]', Gene_ensGene))

    tmp_content_array = []

    if relevant_info_fields.get('GeneDetail.ensGene'):

        GeneDetail_ensGene = convert_escaped_chars(relevant_info_fields.get('GeneDetail.ensGene'))


        if re.match(pattern, GeneDetail_ensGene):
            tmp_content = {}
            tmp_content['ensGene_gene_id'] = gene_id
            tmp_content['ensGene_distance_to_gene'] = GeneDetail_ensGene
            tmp_content_array.append(tmp_content)

        elif ':' in GeneDetail_ensGene:
            for record in GeneDetail_ensGene.split(','):
                tmp_content = {}
                for ele in record.split(':'):
                    if ele.startswith('ENST'):
                        tmp_content['ensGene_transcript_id'] = ele
                    elif ele.startswith('exon'):
                        tmp_content['ensGene_location'] = ele
                    elif ele.startswith('c.'):
                        tmp_content['ensGene_cDNA_change'] = ele

                tmp_content['ensGene_gene_id'] = gene_id

                tmp_content_array.append(tmp_content)

    else:
        tmp_content = {}
        tmp_content['ensGene_gene_id'] = gene_id
        tmp_content_array.append(tmp_content)

    return tmp_content_array

def AAChange_refGene_parser(AAChange_refGene):

    tmp_content_array = []
    AAChange_refGene = AAChange_refGene.split(',')

    for ele in AAChange_refGene:
        if not ele:
            continue
        if ele.lower() == 'unknown':
            continue
        tmp_content = {}
        tmp_tmp = ele.split(':')
        if len(tmp_tmp) == 5:
            tmp_content['refGene_symbol'] = tmp_tmp[0]
            tmp_content['refGene_refgene_id'] = tmp_tmp[1]
            tmp_content['refGene_location'] = tmp_tmp[2]
            tmp_content['refGene_cDNA_change'] = tmp_tmp[3]
            tmp_content['refGene_aa_change'] = tmp_tmp[4]
        elif len(tmp_tmp) == 4:
            tmp_content['refGene_symbol'] = tmp_tmp[0]
            tmp_content['refGene_refgene_id'] = tmp_tmp[1]
            tmp_content['refGene_location'] = tmp_tmp[2]
            tmp_content['refGene_cDNA_change'] = tmp_tmp[3]
        elif len(tmp_tmp) == 3:
            tmp_content['refGene_symbol'] = tmp_tmp[0]
            tmp_content['refGene_refgene_id'] = tmp_tmp[1]
            tmp_content['refGene_location'] = tmp_tmp[2]
        else:
            print(ele)
            raise VCFException('Length of refGene is not 3, 4, or 5')
        tmp_content_array.append(tmp_content)

    return tmp_content_array

def AAChange_ensGene_parser(AAChange_ensGene):

    tmp_content_array = []
    AAChange_ensGene = AAChange_ensGene.split(',')

    for ele in AAChange_ensGene:
        if not ele:
            continue
        if ele.lower() == 'unknown':
            continue
        tmp_content = {}
        tmp_tmp = ele.split(':')
        if len(tmp_tmp) == 5:
            tmp_content['ensGene_gene_id'] = tmp_tmp[0]
            tmp_content['ensGene_transcript_id'] = tmp_tmp[1]
            tmp_content['ensGene_location'] = tmp_tmp[2]
            tmp_content['ensGene_cDNA_change'] = tmp_tmp[3]
            tmp_content['ensGene_aa_change'] = tmp_tmp[4]
        elif len(tmp_tmp) == 4:
            tmp_content['ensGene_gene_id'] = tmp_tmp[0]
            tmp_content['ensGene_transcript_id'] = tmp_tmp[1]
            tmp_content['ensGene_location'] = tmp_tmp[2]
            tmp_content['ensGene_cDNA_change'] = tmp_tmp[3]
        elif len(tmp_tmp) == 3:
            tmp_content['ensGene_gene_id'] = tmp_tmp[0]
            tmp_content['ensGene_transcript_id'] = tmp_tmp[1]
            tmp_content['ensGene_location'] = tmp_tmp[2]
        else:
            print(ele)
            raise VCFException('Length of ensGene is not 3, 4, or 5')
        tmp_content_array.append(tmp_content)

    return tmp_content_array



def convert_escaped_chars(input_string):
    input_string = input_string.replace("\\x3b", ";")
    input_string = input_string.replace("\\x2c", ",")
    input_string = input_string.replace("\\x3d", "=")

    return input_string



#@profile
def set_data(es, index_name, type_name, vcf_filename, vcf_mapping, vcf_label, **kwargs):

    update = kwargs.get('update')


    global GLOBAL_NO_VARIANTS_PROCESSED
    global GLOBAL_NO_VARIANTS_CREATED
    global GLOBAL_NO_VARIANTS_UPDATED


    format_fields = vcf_mapping.get('FORMAT_FIELDS').get('nested_fields')
    fixed_fields = vcf_mapping.get('FIXED_FIELDS')
    info_fields = vcf_mapping.get('INFO_FIELDS')

    int_format_fields = set([key for key in format_fields.keys() if format_fields[key].get('es_field_datatype') == 'integer'])
    float_format_fields = set([key for key in format_fields.keys() if format_fields[key].get('es_field_datatype') == 'float'])

    null_fields = [(key, info_fields[key].get('null_value')) for key in info_fields.keys() if 'null_value' in info_fields[key]]
    overwrite_fields = [(key, info_fields[key].get('overwrites')) for key in info_fields.keys() if 'overwrites' in info_fields[key]]
    exist_only_fields = set([key for key in info_fields.keys() if 'is_exists_only' in info_fields[key]])
    parse_with_fields = {info_fields[key].get('parse_with'): key  for key in info_fields.keys() if 'parse_with' in info_fields[key]}

    fields_to_skip = set(['ALLELE_END', 'ANNOVAR_DATE', 'END',])
    run_dependent_fixed_fields = ['FILTER', 'QUAL']
    run_dependent_info_fields=[
                                'BaseQRankSum',
                                'ClippingRankSum',
                                'DP',
                                # 'FS',
                                'InbreedingCoeff',
                                # 'GQ_MEAN',
                                'MLEAC',
                                'MLEAF',
                                'MQ',
                                'MQ0',
                                'MQRankSum',
                                'QD',
                                'ReadPosRankSum',
                                'SOR',
                                'VQSLOD',
                                'culprit']

    run_dependent_fields = run_dependent_fixed_fields + run_dependent_info_fields + ['sample']

    no_lines = estimate_no_variants_in_file(vcf_filename, 200000)
    # no_lines = 100000
    time_now = datetime.now()
    print('Importing an estimated %d variants into Elasticsearch' %(no_lines))
    header_found = False
    exception_vcf_line_io_mode = 'w'
    with open(vcf_filename, 'r') as fp:
        for line in tqdm(fp, total=no_lines):
        # for no_line, line in enumerate(fp, 1):
            line = line.strip()

            if GLOBAL_NO_VARIANTS_PROCESSED > no_lines:
                break

            if line.startswith('##'):
                continue

            if not header_found:
                if line.startswith('#CHROM'):
                    line = line[1:]
                    header = line.split('\t')
                    sample_start = header.index('FORMAT') + 1
                    samples = header[sample_start:]
                    header_found = True
                    continue


            data = dict(zip(header, line.split('\t')))
            info = data['INFO'].split(';')



            info_dict = {}
            for ele in info:
                if ele.split('=')[0] in fields_to_skip:
                    continue
                if '=' in ele:
                    key, val = (ele.split('=')[0], ''.join(ele.split('=')[1:]))
                    if val != '.':
                        info_dict[key] = convert_escaped_chars(val)
                else:
                    info_dict[ele] = True

            content = {}

            try:
                CHROM = data['CHROM']
                POS = int(data['POS'])
                REF = data['REF']
                ALT = data['ALT']
                ID = data['ID']



                content['CHROM'] = CHROM
                content['POS'] = POS
                content['REF'] = REF
                content['ALT'] = ALT
                if ID != '.':
                    content['ID'] = data['ID']


                es_id = get_es_id(CHROM, POS, REF, ALT, index_name, type_name)

                fields_to_update = None
                if update:
                    es_id_exists = es.exists(index=index_name, doc_type=type_name, id=es_id)
                    if es_id_exists:
                        fields_to_update = es.get(index=index_name, doc_type=type_name, id=es_id, _source_include=run_dependent_fields)['_source']

                ### Samples
                sample_array = deque()
                FORMAT = data['FORMAT']
                format_fields_for_current_line = FORMAT.split(':')
                gt_location = format_fields_for_current_line.index('GT')
                for sample in samples:
                    # pass
                    sample_content = {}
                    sample_values = data.get(sample)
                    sample_values = sample_values.split(':')

                    if sample_values[gt_location] in ['./.', '0/0', '0|0']:
                        continue

                    sample_content['sample_ID'] = sample

                    for idx, key_format_field in enumerate(format_fields_for_current_line):
                        key_format_field_sample = 'sample_%s' %(key_format_field)
                        key_value = sample_values[idx]
                        if key_format_field in int_format_fields:
                            if ',' in key_value:
                                sample_content[key_format_field_sample] = [int(s_val) for s_val in key_value.split(',')]
                            else:
                                if key_value not in ['.']:
                                    sample_content[key_format_field_sample] = int(key_value)


                        elif key_format_field in float_format_fields:
                            if ',' in key_value:
                                sample_content[key_format_field_sample] = [float(s_val) for s_val in key_value.split(',') if not math.isnan(float(s_val))]
                            else:
                                if key_value not in ['.'] and not math.isnan(float(key_value)):
                                    sample_content[key_format_field_sample] = float(key_value)
                        else:
                            if key_value not in ['.']:
                                sample_content[key_format_field_sample] = key_value



                    if not vcf_label == 'None':
                        sample_content['sample_label'] = vcf_label
                    sample_array.appendleft(sample_content)

                if fields_to_update:
                    GLOBAL_NO_VARIANTS_UPDATED += 1
                    GLOBAL_NO_VARIANTS_PROCESSED += 1

                    fields_to_update['sample'].extend(sample_array)

                    if vcf_label != 'None':
                        AC_label = 'AC_%s' %(vcf_label)
                        AF_label = 'AF_%s' %(vcf_label)
                        AN_label = 'AN_%s' %(vcf_label)
                        fields_to_update[AC_label] = int(info_dict.get('AC'))
                        fields_to_update[AF_label] = float(info_dict.get('AF'))
                        fields_to_update[AN_label] = int(info_dict.get('AN'))
                        fields_to_update['FILTER'].extend([{'FILTER_label': vcf_label, 'FILTER_status': data['FILTER']}])
                        if not math.isnan(float(data['QUAL'])):
                            fields_to_update['QUAL'].extend([{'QUAL_label': vcf_label, 'QUAL_score': float(data['QUAL'])}])
                        for field in run_dependent_info_fields:
                            if not info_dict.get(field):
                                continue
                            if info_dict[field] == 'nan':
                                continue
                            label_field_name = "%s_label" %(field)
                            value_field_name = "%s_value" %(field)
                            es_field_datatype =  info_fields[field]['nested_fields'][value_field_name]['es_field_datatype']
                            if not fields_to_update.get(field):
                                fields_to_update[field] = []
                            if es_field_datatype == 'integer':
                                fields_to_update[field].extend([{label_field_name: vcf_label, value_field_name: int(info_dict[field])}])
                            elif es_field_datatype == 'float' and not math.isnan(float(info_dict[field])):
                                fields_to_update[field].extend([{label_field_name: vcf_label, value_field_name: float(info_dict[field])}])
                            else:
                                fields_to_update[field].extend([{label_field_name: vcf_label, value_field_name: info_dict[field]}])

                    else:
                        fields_to_update['FILTER'].extend([{'FILTER_status': data['FILTER']}])
                        if not math.isnan(float(data['QUAL'])):
                            fields_to_update['QUAL'].extend([{'QUAL_score': float(data['QUAL'])}])
                        for field in run_dependent_info_fields:
                            if not info_dict.get(field):
                                continue
                            if info_dict[field] == 'nan':
                                continue
                            value_field_name = "%s_value" %(field)
                            es_field_datatype =  info_fields[field]['nested_fields'][value_field_name]['es_field_datatype']
                            if not fields_to_update.get(field):
                                fields_to_update[field] = []
                            if es_field_datatype == 'integer':
                                fields_to_update[field].extend([{value_field_name: int(info_dict[field])}])
                            elif es_field_datatype == 'float' and not math.isnan(float(info_dict[field])):
                                fields_to_update[field].extend([{value_field_name: float(info_dict[field])}])
                            else:
                                fields_to_update[field].extend([{value_field_name: info_dict[field]}])

                    #{ "update" : {"_id" : "1", "_type" : "type1", "_index" : "test"} }
                    #{ "doc" : {"field2" : "value2"} }
                    yield json.dumps({ "update" : {"_id" : es_id} })
                    # yield '{"update" : {"_id" : "%s", "_type" : "%s", "_index" : "%s"}}' %(es_id, type_name, index_name)
                    yield json.dumps({"doc": fields_to_update})

                    continue

                if sample_array:
                    # pprint(sample_array)
                    content['sample'] = list(sample_array)


                if ALT == '.':
                    content['VariantType'] = 'INDEL'
                elif len(ALT) == 1 and len(REF) == 1 and ALT != '.' and REF != '.':
                    content['VariantType'] = 'SNV'
                else:
                    content['VariantType'] = 'INDEL'

                content['Variant'] = '%s-%d-%s-%s' %(CHROM, POS, REF[:10], ALT[:10])

                if vcf_label != 'None':
                    ## this requires fixing!!!
                    AC_label = 'AC_%s' %(vcf_label)
                    AF_label = 'AF_%s' %(vcf_label)
                    AN_label = 'AN_%s' %(vcf_label)
                    info_dict[AC_label] = (info_dict.pop('AC'))
                    info_dict[AF_label] = (info_dict.pop('AF'))
                    info_dict[AN_label] = (info_dict.pop('AN'))
                    content['FILTER'] = [{'FILTER_label': vcf_label, 'FILTER_status': data['FILTER']}]
                    if not math.isnan(float(data['QUAL'])):
                        content['QUAL'] = [{'QUAL_label': vcf_label, 'QUAL_score': float(data['QUAL'])}]
                    for field in run_dependent_info_fields:
                        if not info_dict.get(field):
                            continue
                        if info_dict[field] == 'nan':
                            continue
                        label_field_name = "%s_label" %(field)
                        value_field_name = "%s_value" %(field)
                        es_field_datatype =  info_fields[field]['nested_fields'][value_field_name]['es_field_datatype']
                        if es_field_datatype == 'integer':
                            content[field] = [{label_field_name: vcf_label, value_field_name: int(info_dict[field])}]
                        elif es_field_datatype == 'float' and not math.isnan(float(info_dict[field])):
                            content[field] = [{label_field_name: vcf_label, value_field_name: float(info_dict[field])}]
                        else:
                            content[field] = [{label_field_name: vcf_label, value_field_name: info_dict[field]}]
                else:
                    content['FILTER'] = [{'FILTER_status': data['FILTER']}]
                    if not math.isnan(float(data['QUAL'])):
                        content['QUAL'] = [{'QUAL_score': float(data['QUAL'])}]
                    for field in run_dependent_info_fields:
                        if not info_dict.get(field):
                            continue
                        if info_dict[field] == 'nan':
                            continue
                        value_field_name = "%s_value" %(field)
                        es_field_datatype =  info_fields[field]['nested_fields'][value_field_name]['es_field_datatype']
                        if es_field_datatype == 'integer':
                            content[field] = [{value_field_name: int(info_dict[field])}]
                        elif es_field_datatype == 'float' and not math.isnan(float(info_dict[field])):
                            content[field] = [{value_field_name: float(info_dict[field])}]
                        else:
                            content[field] = [{value_field_name: info_dict[field]}]

                for key, val in null_fields:
                    content[key] = val

                for info_key in info_fields.keys():

                    if info_key in fields_to_skip:
                        continue

                    if info_fields[info_key].get('is_nested_label_field'):
                        continue

                    if not info_dict.get(info_key):
                        continue


                    es_field_name = info_fields[info_key].get('es_field_name', '')
                    es_field_datatype = info_fields[info_key].get('es_field_datatype', '')

                    if info_key in exist_only_fields and es_field_datatype == 'boolean':
                        content[es_field_name] = True
                        continue

                    val = info_dict.get(info_key)
                    if val == 'nan':
                        continue

                    if es_field_datatype == 'integer':
                        if ',' in val:
                            val = [int(ele) for ele in val.split(',')]
                        else:
                            val = int(val)
                        content[es_field_name] = val
                        continue
                    elif es_field_datatype == 'float':
                        if ',' in val:
                            val = [float(ele) for ele in val.split(',') if not math.isnan(float(ele))]
                        else:
                            val = float(val)
                            if not math.isnan(val):
                                content[es_field_name] = val
                        continue
                    elif es_field_datatype in ['keyword', 'text'] :
                        if info_fields[info_key].get('value_mapping'):
                            value_mapping = info_fields[info_key].get('value_mapping')
                            val = value_mapping.get(val, val)

                        if info_fields[info_key].get('parse_function'):
                            parse_function = eval(info_fields[info_key].get('parse_function'))
                            val = parse_function(val)
                            content[es_field_name] = val
                            continue
                        else:
                            content[es_field_name] = val
                            continue



                    ### deal with nested fields
                    if info_fields[info_key].get('shares_nested_path'):
                        # print(info_key)
                        shares_nested_path = info_fields[info_key].get('shares_nested_path')
                        es_field_name = info_fields[shares_nested_path].get('es_nested_path')
                        parse_function = eval(info_fields[info_key].get('parse_function'))
                        # print(es_field_name, val, parse_function)
                        val = {info_key: val}

                        if parse_with_fields.get(info_key):
                            parse_with_field_name = parse_with_fields.get(info_key)
                            val.update({parse_with_field_name: info_dict.get(parse_with_field_name)})
                        val = parse_function(val)
                        # print(es_field_name, val)
                        if es_field_name in content:
                            content[es_field_name].extend(val)
                            continue
                        else:
                            content[es_field_name] = val
                            continue

                    clinvar_input_dict = {}
                    if info_fields[info_key].get('es_nested_path'):

                        ## special case for clinvar
                        if info_key == 'CLNDBN' and val != '.':
                            clinvar_input_dict = {
                                'CLINSIG' : info_dict['CLINSIG'],
                                'CLNACC' : info_dict['CLNACC'],
                                'CLNDBN' : info_dict['CLNDBN'],
                                'CLNDSDB' : info_dict['CLNDSDB'],
                                'CLNDSDBID' : info_dict['CLNDSDBID'],
                            }
                            clinvar_output_dict = clinvar_parser(clinvar_input_dict)
                            content['clinvar'] = clinvar_output_dict
                            continue
                        elif info_key in ['CLNACC', 'CLINSIG', 'CLNDSDB', 'CLNDSDBID']:
                            continue

                        parse_function = eval(info_fields[info_key].get('parse_function'))
                        es_field_name = info_fields[info_key].get('es_nested_path')
                        val = parse_function(val)
                        if es_field_name in content:
                            content[es_field_name].extend(val)
                            continue
                        else:
                            content[es_field_name] = val
                            continue


                for overwrite_key, orig_key in overwrite_fields:
                    es_overwrite_key = info_fields[overwrite_key].get('es_field_name')
                    es_orig_key = info_fields[orig_key].get('es_field_name')
                    if es_overwrite_key in content:
                        content[es_orig_key] = content[es_overwrite_key]


                content['refGene'] = prune_array('refGene_symbol', content['refGene'])
                content['ensGene'] = prune_array('ensGene_gene_id', content['ensGene'])

                GLOBAL_NO_VARIANTS_CREATED += 1
                GLOBAL_NO_VARIANTS_PROCESSED += 1


                #{ "index" : { "_index" : "test", "_type" : "type1", "_id" : "1" } }
                #{ "field1" : "value1" }
                yield json.dumps({ "index" : {"_id" : es_id } })
                yield json.dumps(content)


            except Exception as e:
                print(info_key, es_field_datatype, val)
                print('Error on line {}'.format(sys.exc_info()[-1].tb_lineno), type(e).__name__, e)
                # print(line)


def main():
    global GLOBAL_NO_VARIANTS_PROCESSED
    global GLOBAL_NO_VARIANTS_CREATED
    global GLOBAL_NO_VARIANTS_UPDATED



    start_time = datetime.now()

    parser = argparse.ArgumentParser()
    required = parser.add_argument_group('required named arguments')
    required.add_argument("--hostname", help="Elasticsearch hostname", required=True)
    required.add_argument("--port", type=int, help="Elasticsearch port", required=True)
    required.add_argument("--index", help="Elasticsearch index name", required=True)
    required.add_argument("--type", help="Elasticsearch doc type name", required=True)
    required.add_argument("--label", help="Cohort labels, e.g., \"control, case\" or \"None\"", required=True)
    required.add_argument("--update", help="Initial Import, e.g., \"True\" or \"False\"", required=True)
    required.add_argument("--vcf", help="VCF file to import", required=True)
    required.add_argument("--mapping", help="VCF mapping", required=True)
    args = parser.parse_args()



    if not os.path.exists(args.vcf):
        raise IOError("VCF file does not exist at location: %s" %(args.vcf))

    if not os.path.exists(args.mapping):
        raise IOError("VCF information file does not exist at location: %s" %(args.mapping))

    # --hostname 199.109.192.65
    # --port 9200
    # --index sim
    # --type wes
    # --label None
    # --vcf 20170419_SIM_WES_CASE.hg19_multianno.vcf
    # --mapping inspect_output_for_sim_wes.txt

    vcf_label = args.label
    vcf_filename = args.vcf
    vcf_mapping = json.load(open(args.mapping, 'r'))
    update = args.update
    if update == 'True':
        update = True
    elif update == 'False':
        update = False

    es = elasticsearch.Elasticsearch(host=args.hostname, port=args.port)
    index_name = args.index
    type_name = args.type
    es.cluster.health(wait_for_status='yellow')
    es.indices.put_settings(index=index_name, body={"refresh_interval": "-1"})



    file_count = 1
    file_size_total = 0
    directory_name = tempfile.mkdtemp()
    data_available = False
    for line_count, data in enumerate(set_data(es, index_name,
                        type_name,
                        vcf_filename,
                        vcf_mapping,
                        vcf_label,
                        update=update), 1):

        file_size_total += sys.getsizeof(data)

        if line_count == 1:
            filename = os.path.join(directory_name, f'output_{file_count}.json')
            output_file = open(filename, 'w')


        output_file.write(f'{data}\n')
        data_available = True

        if file_size_total > 83886080 and ((line_count % 2) == 0):
            output_file.close()
            post_data.delay(args.hostname, args.port, index_name, type_name, filename)
            #
            file_count += 1
            filename = os.path.join(directory_name, f'output_{file_count}.json')
            output_file = open(filename, 'w')
            file_size_total = 0
            data_available = False



    #finally:
    if data_available:
        output_file.close()
        post_data.delay(args.hostname, args.port, index_name, type_name, filename)

    time.sleep(60)
    update_refresh_interval.delay(args.hostname, args.port, index_name, '30s')
        # pprint(data)
        # es.index(index=index_name, doc_type=type_name, body=data)




    vcf_import_end_time = datetime.now()

    end_time = datetime.now()
    sys.stdout.flush()
    print('\nVCF import started at %s' %(start_time))
    print('VCF import ended at %s' %(vcf_import_end_time))
    print('VCF importing took %s' %(vcf_import_end_time-start_time))
    # if update:
    #     print('Elasticsearch indexing took %s' %(end_time-vcf_import_end_time))
    print('Importing and indexing VCF took %s' %(end_time-start_time))
    print("Number of variants processed:", GLOBAL_NO_VARIANTS_PROCESSED)
    print("Number of variants created:", GLOBAL_NO_VARIANTS_CREATED)
    print("Number of variants updated:", GLOBAL_NO_VARIANTS_UPDATED)




if __name__ == "__main__":
    main()





