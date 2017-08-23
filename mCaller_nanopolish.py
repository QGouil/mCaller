#!/usr/bin/env python
#A program to classify bases as methylated or unmethylated based on long-range signals using the output from nanopolish
#Alexa McIntyre, 2016-2017

from collections import defaultdict
import numpy as np
import pickle 
import sys
#import time
import os
import math
import multiprocessing
from Bio import SeqIO

from extract_contexts import *
from train_model import train_classifier,pos2label
from read_qual import extract_read_quality

def distribute_threads(positions_list,motif,tsvname,read2qual,refname,num_refs,base,mod,nprocs,nvariables,train,modelfile,skip_thresh,qual_thresh,classifier):
    """ distributes list of genomic positions across processes then adds resulting signals to matrix if training"""
    if not train:
      tsv_output = '.'.join(tsvname.split('.')[:-1])+'.diffs.'+str(nvariables)
      training_pos_dict = None
    else: 
      tsv_output = '.'.join(tsvname.split('.')[:-1])+'.diffs.'+str(nvariables)+'.train'
      training_pos_dict = pos2label(positions_list)
    try:
       os.remove(tsv_output)
    except OSError:
       pass

    print(num_refs, 'contigs')
    print(nprocs, 'threads')

    if nprocs > 1:
        nprocs_allocated = 0
        procs = []
        out_q = multiprocessing.Queue()
        def worker(out_q,tsvname,fastaname,read2qual,nvariables,skip_thresh,qual_thresh,modelfile,classifier,startline,endline,train,training_pos_dict,contigid=None,meth_fwd=None,meth_rev=None,base=base,motif=None,positions_list=None): 
            #extract_features(tsv_input,fasta_input,read2qual,k,skip_thresh,qual_thresh,modelfile,classifier,startline,endline=None,train=False,pos_label=None)
            outtup = extract_features(tsvname,fastaname,read2qual,nvariables,skip_thresh,qual_thresh,modelfile,classifier,startline,endline=endline,train=train,pos_label=training_pos_dict,chrom=contigid,meth_fwd=meth_fwd,meth_rev=meth_rev,base=base,motif=motif,positions_list=positions_list)
            out_q.put(outtup)
        def countlines(filename,num_refs,contigid=None):
            if num_refs == 0: #TODO: remove this
                print(filename)
                return sum(1 for _ in open(filename))
            else:
                contig_reached = False
                contigstart = 1
                contignum = 0
                for line in open(filename):
                    if line.split('\t')[0] == contigid:
                        if not contig_reached:
                            contig_reached = True
                        contignum += 1
                    elif not contig_reached:
                        contigstart += 1
                return contigstart,contignum

    if nprocs == 1 or num_refs == 1:
        for ref in SeqIO.parse(refname,"fasta"):
            contigid = ref.id
            print('contig =',contigid,'- allocating',nprocs,'threads')
            meth_fwd,meth_rev = methylate_references(str(ref.seq).upper(),base,motif=motif,positions=positions_list)
            #sys.exit(0)

            if nprocs == 1:   
                if not train:
                    extract_features(tsvname,refname,read2qual,nvariables,skip_thresh,qual_thresh,modelfile,classifier,0,chrom=contigid,meth_fwd=meth_fwd,meth_rev=meth_rev) #TODO: implement quality thresholding
                else:
                    signal_mat, label_array, context_array = extract_features(tsvname,refname,read2qual,nvariables,skip_thresh,qual_thresh,modelfile,classifier,0,train=train,pos_label=training_pos_dict,chrom=contigid,meth_fwd=meth_fwd,meth_rev=meth_rev)

            else:
                print(tsvname)
                cstart, nlines = countlines(tsvname,num_refs,contigid) #TODO: split by reference sequence in or out of python? 
                chunksize = int(math.ceil(nlines / float(nprocs)))

                for i in range(nprocs):
                    p = multiprocessing.Process(
                        target=worker,
                        args=(out_q,tsvname,refname,read2qual,nvariables,skip_thresh,qual_thresh,modelfile,classifier,cstart+chunksize*i,cstart+chunksize*(i+1),train,training_pos_dict,contigid,meth_fwd,meth_rev))
                    procs.append(p)
                    p.start()

    else:
        nlines = countlines(tsvname,1)
        chunksize = int(math.ceil(nlines / float(nprocs)))

        for i in range(nprocs):
            p = multiprocessing.Process(
                    target=worker, 
                    args=(out_q,tsvname,refname,read2qual,nvariables,skip_thresh,qual_thresh,modelfile,classifier,chunksize*i,chunksize*(i+1),train,training_pos_dict,None,None,None,base,motif,positions_list))
            procs.append(p)
            p.start()

    if nprocs > 1:
        if train:
            # Collect all results into a signal matrix and an array of labels
            signal_mat = []
            label_array = []
            context_array = []
            for i,proc in enumerate(procs):
                tmp_signal_mat,tmp_label_array,tmp_contexts = out_q.get()
                print('updating with results from process',i)
                signal_mat.extend(tmp_signal_mat)
                label_array.extend(tmp_label_array)
                context_array.extend(tmp_contexts)

             # Wait for all worker processes to finish
        for p in procs:
            p.join ()
       
    print('Finished extracting signals')

    if train: 
       assert len(label_array) > 5, 'insufficient data aligned to labeled positions for training'
       train_classifier(signal_mat,label_array,context_array,modelfile,classifier) 
       print('Finished training') 


def main():
    #parse command line options
    from argparse import ArgumentParser
    parser = ArgumentParser(description='Classify bases as methylated or unmethylated')
    all_or_some = parser.add_mutually_exclusive_group(required=True)
    all_or_some.add_argument('-p','--positions',type=str,required=False, help='file with a list of positions at which to classify bases (must be formatted as space- or tab-separated file with chromosome, position, strand, and label if training)')
    all_or_some.add_argument('-m','--motif',type=str,required=False, help='classify every base of type --base in the motif specified instead (can be single one-mer)')
    parser.add_argument('-r','--reference',type=str,required=True,help='fasta file with reference aligned to')
    parser.add_argument('-e','--tsv',type=str,required=True,help='tsv file with nanopolish event alignment')
    parser.add_argument('-f','--fastq',type=str,required=True,help='fastq file with nanopore reads')
    parser.add_argument('-t','--threads',type=int,required=False,help='specify number of processes (default = 1)',default=1)
    parser.add_argument('-b','--base',type=str,required=False,help='bases to classify as methylated or unmethylated (A or C, default A)',default='A')
    parser.add_argument('-n','--num_variables',type=int,required=False,help='change the length of the context used to classify (default of 6 variables corresponds to 11-mer context (6*2-1))',default=6)
    parser.add_argument('--train',action='store_true',required=False,help='train a new model (requires labels in positions file)',default=False)
    parser.add_argument('-d','--modelfile',type=str,required=False,help='model file name')
    parser.add_argument('-s','--skip_thresh',type=int,required=False,help='number of skips to allow within an observation (default 0)',default=0)
    parser.add_argument('-q','--qual_thresh',type=float,required=False,help='quality threshold for reads (under development, please sort your own reads for now)',default=0)
    parser.add_argument('-c','--classifier',type=str,required=False,help='use alternative classifier: options = NN (default) RF, LR, or NBC',default='NN')
    parser.add_argument('-v','--version',action='store_true',required=False,help='print version')
    args = parser.parse_args()

    if args.version:
        print('mCallerNP 0.1')
        sys.exit(0)

    if args.base == 'A':
        mod = 'm6A'
    elif args.base == 'C':
        mod = 'm5C' #TODO: test m4C
    else: 
        print('classification only available for A or C bases so far') 
        sys.exit(0)
 
    if not args.modelfile:
        modelfile = 'model_'+args.classifier+'_'+str(args.num_variables)+'_'+mod+'.pkl'
    else:
        modelfile = args.modelfile
    
    if not args.train:
        assert os.path.isfile(args.modelfile), 'model file not found at '+args.modelfile

    if args.motif and len(args.motif) == 1:
        base = args.motif
    else:
        base = args.base

    assert (args.skip_thresh < args.num_variables/2), 'too many skips with only '+str(args.num_variables)+' variables - try < half' 

    assert os.path.isfile(args.fastq), 'fastq file not found at '+args.fastq
    read2qual = extract_read_quality(args.fastq)

    try:
        num_refs = 0
        for ref in SeqIO.parse(args.reference,"fasta"):
            num_refs+=1
    except IOError:
        print('reference file missing')
        sys.exit(0)

    #distribute to multiple threads for main computations
    distribute_threads(args.positions,args.motif,args.tsv,read2qual,args.reference,num_refs,base,mod,args.threads,args.num_variables,
        args.train,modelfile,args.skip_thresh,args.qual_thresh,args.classifier)

if __name__ == "__main__":
    main()


