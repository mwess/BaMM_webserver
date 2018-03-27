import time
from shutil import copyfile
import uuid
import os
from os import path
import logging

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.core.files import File
from django.utils import timezone

from bammmotif.peng.tasks import peng_seeding_pipeline
from bammmotif.bamm.tasks import bamm_refinement_pipeline
from bammmotif.bammscan.tasks import bamm_scan_pipeline
from bammmotif.mmcompare.tasks import mmcompare_pipeline
from bammmotif.models import JobInfo, PengJob, BaMMJob, BaMMScanJob, MMcompareJob, MotifDatabase
from bammmotif.utils import get_job_output_folder, filename_relative_to_job_dir
from bammmotif.peng.cmd_modules import ShootPengModule
from bammmotif.utils.meme_reader import get_n_motifs, get_motif_ids
from bammmotif.peng.utils import save_selected_motifs, copy_peng_to_bamm
from bammmotif.peng.io import get_motif_init_file, job_input_directory, mmcompare_motif_init_file
from bammmotif.bamm.utils import (
    upload_example_fasta,
    upload_example_motif,
)
from bammmotif.utils import (
    is_fasta,
    remove_job_folder,
)
from webserver.settings import EXAMPLE_DIR
from bammmotif.peng.settings import FILTERPWM_OUTPUT_FILE


INITIAL_EXAMPLE_UUID = 0
EXAMPLE_MOTIF_DB = os.environ['EXAMPLE_MOTIF_DB']
logger = logging.getLogger(__name__)


def example_in_db(example_file):
    example_user = get_object_or_404(User, username='Example_User')
    example_jobs = JobInfo.objects.filter(user=example_user, job_name=os.path.basename(example_file))
    return len(example_jobs) > 0


def already_done(job_id):
    job_uuid = uuid.UUID(int=job_id)
    job = JobInfo.objects.filter(pk=job_uuid).first()
    if job:
        if not job.complete:
            logger.info('Job %s not finished. Cleaning up and restarting', job)
            job.delete()
            remove_job_folder(job_uuid)
            return False
        logger.debug('Job %s already done. Skipping', job)
        return True
    else:
        return False


def new_example_job_info(next_id, job_type, fname):
    example_user = get_object_or_404(User, username='Example_User')
    job_info = JobInfo(
        job_id=uuid.UUID(int=next_id),
        job_name="example run",
        mode='Prediction',
        complete=False,
        job_type=job_type,
        user=example_user,
    )
    return job_info, next_id + 1

def new_peng_job(next_id, example_file):
    job_info, next_id = new_example_job_info(next_id, 'peng', example_file)
    job_info.save()
    output_dir = get_job_output_folder(job_info.pk)
    peng_job = PengJob(
        meta_job=job_info,
        meme_output=os.path.join(output_dir, ShootPengModule.defaults['meme_output']),
        json_output=os.path.join(output_dir, ShootPengModule.defaults['json_output']),
    )
    with open(example_file) as fh:
        peng_job.fasta_file.save(os.path.basename(example_file), File(fh))
    peng_job.save()
    peng_seeding_pipeline.delay(peng_job.meta_job.pk)
    while not job_info.complete:
        job_info = get_object_or_404(JobInfo, pk=job_info.pk) # Kind of dirt.
        time.sleep(10)
    return next_id, peng_job

def new_bamm_job(next_id, example_file, peng_job):
    job_info, next_id = new_example_job_info(next_id, 'bamm', example_file)
    job_info.save()
    mdb = get_object_or_404(MotifDatabase, db_id=EXAMPLE_MOTIF_DB)
    bamm_job = BaMMJob(
        meta_job=job_info,
        Motif_Initialization="Custom File",
        Motif_Init_File_Format="PWM",
        MMcompare=True,
        score_Seqset=True,
        FDR=True,
        peng_job=peng_job,
        motif_db=mdb,
    )
    with open(example_file) as fh:
        bamm_job.Input_Sequences.save(os.path.basename(example_file), File(fh))

    n_motifs = 3
    meme_file = path.join(get_job_output_folder(peng_job.pk), FILTERPWM_OUTPUT_FILE)
    motifs = get_motif_ids(meme_file)[:n_motifs]
    save_selected_motifs(motifs, peng_job.pk, bamm_job.meta_job.pk)
    copy_peng_to_bamm(peng_job.pk, bamm_job.pk, motifs)
    bamm_job.num_init_motifs = len(motifs)
    bamm_job.num_motifs = len(motifs)
    bamm_job.Motif_InitFile.name = get_motif_init_file(str(bamm_job.pk))
    bamm_job.Motif_Initialization = "Custom File"
    bamm_job.Motif_Init_File_Format = "PWM"
    bamm_job.score_Cutoff = 0.0001
    bamm_job.save()
    bamm_refinement_pipeline.delay(bamm_job.meta_job.pk)
    while not job_info.complete:
        job_info = get_object_or_404(JobInfo, pk=job_info.pk)
        time.sleep(10)
    return next_id, bamm_job.pk

def new_bammscan_job(next_id, example_file, bamm_id):
    job_info, next_id = new_example_job_info(next_id, 'scan', example_file)
    job_info.save()
    mdb = get_object_or_404(MotifDatabase, db_id=EXAMPLE_MOTIF_DB)
    bamm_scan_job = BaMMScanJob(
        meta_job=job_info,
        motif_db=mdb,
        MMcompare=True,
        Motif_Init_File_Format="PWM",
    )
    with open(example_file) as fh:
        bamm_scan_job.Input_Sequences.save(os.path.basename(example_file), File(fh))
    bamm_scan_job.Motif_InitFile.name = get_motif_init_file(str(bamm_id))
    bamm_scan_job.score_Cutoff = 0.0001
    bamm_scan_job.save()
    bamm_scan_pipeline.delay(bamm_scan_job.meta_job.pk)
    while not job_info.complete:
        job_info = get_object_or_404(JobInfo, pk=job_info.pk) # Kind of dirt.
        time.sleep(10)
    return next_id

def new_mmcompare_job(next_id, example_file, bamm_id):
    job_info, next_id = new_example_job_info(next_id, 'compare', example_file)
    job_info.save()
    mdb = get_object_or_404(MotifDatabase, db_id=EXAMPLE_MOTIF_DB)
    mmcompare_job = MMcompareJob(
        meta_job=job_info,
        motif_db=mdb,
        Motif_Init_File_Format="PWM"
        # Fill in missing data
    )
    # Init motif_init_file
    motif_init_file_src = get_motif_init_file(str(bamm_id))
    motif_init_file_dst = mmcompare_motif_init_file(str(mmcompare_job.pk))
    motif_init_directory = os.path.splitext(motif_init_file_dst)[0]
    if not os.path.exists(motif_init_directory):
        os.makedirs(motif_init_directory)
    copyfile(motif_init_file_src, motif_init_file_dst)
    init_bname = path.basename(motif_init_file_dst)
    mmcompare_job.Motif_InitFile = filename_relative_to_job_dir(mmcompare_job, init_bname)
    mmcompare_job.save()
    mmcompare_pipeline.delay(mmcompare_job.meta_job.pk)
    while not job_info.complete:
        job_info = get_object_or_404(JobInfo, pk=job_info.pk)
        time.sleep(10)
    return next_id


def add_example_to_db(example_file, next_id):
    if already_done(next_id):
        peng_job = PengJob.objects.get(pk=uuid.UUID(int=next_id))
        next_id += 1
    else:
        next_id, peng_job = new_peng_job(next_id, example_file)

    if already_done(next_id):
        bamm_id = uuid.UUID(int=next_id)
        next_id += 1
    else:
        next_id, bamm_id = new_bamm_job(next_id, example_file, peng_job)

    if already_done(next_id):
        next_id += 1
    else:
        next_id = new_bammscan_job(next_id, example_file, bamm_id)

    if already_done(next_id):
        next_id += 1
    else:
        next_id = new_mmcompare_job(next_id, example_file, bamm_id)
    return next_id


class Command(BaseCommand):

    def add_arguments(self, parser):
        parser.add_argument('--flush', action='store_true')

    def handle(self, *args, **options):

        # Check if user exists in db
        user_query = User.objects.filter(username='Example_User')
        if not user_query.exists():
            user = User(username='Example_User', first_name='User', last_name='Example')
            user.set_unusable_password()
            user.save()
        else:
            user = user_query.first()
            
        if options['flush']:
            for example_job in JobInfo.objects.filter(user=user):
                logger.info('Removing example job %s', example_job)
                remove_job_folder(example_job.pk)
                example_job.delete()

        example_list = [os.path.join(EXAMPLE_DIR, x) for x in os.listdir(EXAMPLE_DIR) if is_fasta(x)]
        next_id = INITIAL_EXAMPLE_UUID
        for example in example_list:
            next_id = add_example_to_db(example, next_id)
