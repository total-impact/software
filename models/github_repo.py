from app import db
from app import github_zip_queue
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import or_

from models import github_api
from models.github_api import username_and_repo_name_from_github_url
from models.github_api import github_zip_getter_factory

from models import github_api
import requests
from util import elapsed
from time import time
from time import sleep
import subprocess


class GithubRepo(db.Model):
    login = db.Column(db.Text, primary_key=True)
    repo_name = db.Column(db.Text, primary_key=True)
    language = db.Column(db.Text)
    api_raw = db.Column(JSONB)
    dependency_lines = db.Column(db.Text)
    zip_download_elapsed = db.Column(db.Float)
    zip_download_size = db.Column(db.Integer)
    zip_download_error = db.Column(db.Text)
    zip_grep_elapsed = db.Column(db.Float)

    def __repr__(self):
        return u'<GithubRepo {language} {login}/{repo_name}>'.format(
            language=self.language, login=self.login, repo_name=self.repo_name)

    def set_github_about(self):
        self.api_raw = github_api.get_repo_data(self.login, self.repo_name)
        return self.api_raw

    def set_github_dependency_lines(self):

        getter = github_zip_getter_factory(self.login, self.repo_name)
        getter.get_dep_lines(self.language)

        self.dependency_lines = getter.dep_lines
        self.zip_download_elapsed = getter.download_elapsed
        self.zip_download_size = getter.download_kb
        self.zip_download_error = getter.error
        self.zip_grep_elapsed = getter.grep_elapsed

        return self.dependency_lines


    @property
    def full_name(self):
        return self.login + "/" + self.repo_name


# call python main.py add_python_repos_from_google_bucket to run
def add_python_repos_from_google_bucket():

    url = "https://storage.googleapis.com/impactstory/github_python_repo_names.csv"
    add_repos_from_remote_csv(url, "python")


# call python main.py add_r_repos_from_google_bucket to run
def add_r_repos_from_google_bucket():

    url = "https://storage.googleapis.com/impactstory/github_r_repo_names.csv"
    add_repos_from_remote_csv(url, "r")



def add_repos_from_remote_csv(csv_url, language):
    start = time()

    print "going to go get file"
    response = requests.get(csv_url, stream=True)
    index = 0

    for github_url in response.iter_lines(chunk_size=1000):
        login, repo_name = username_and_repo_name_from_github_url(github_url)
        if login and repo_name:
            repo = GithubRepo(
                login=login,
                repo_name=repo_name, 
                language=language
            )
            print repo
            db.session.merge(repo)
            index += 1
            if index % 1000 == 0:
                db.session.commit()
                print "flushing on index {index}, elapsed: {elapsed}".format(
                    index=index, 
                    elapsed=elapsed(start))

    db.session.commit()



"""
add github about api call
"""
def add_github_about(login, repo_name):
    repo = db.session.query(GithubRepo).get((login, repo_name))
    repo.set_github_about()
    db.session.commit()

    print repo

def add_all_github_about():
    q = db.session.query(GithubRepo.login, GithubRepo.repo_name)
    q = q.filter(GithubRepo.api_raw == 'null')
    q = q.order_by(GithubRepo.login)

    for row in q.all():
        #print "setting this row", row
        add_github_about(row[0], row[1])



"""
add github dependency lines
"""
def add_github_dependency_lines(login, repo_name):
    repo = db.session.query(GithubRepo).get((login, repo_name))
    if repo is None:
        print "there's no repo called {}/{}".format(login, repo_name)
        return False

    repo.set_github_dependency_lines()
    db.session.commit()
    return None  # important that it returns None for RQ


def add_all_github_dependency_lines():
    empty_github_zip_queue()
    num_jobs = 100*1000

    print "querying the db for {} repos...".format(num_jobs)
    query_start = time()

    q = db.session.query(GithubRepo.login, GithubRepo.repo_name)
    q = q.filter(~GithubRepo.api_raw.has_key('error_code'))
    q = q.filter(GithubRepo.dependency_lines == None, 
        GithubRepo.zip_download_error == None, 
        GithubRepo.zip_download_elapsed == None)
    q = q.order_by(GithubRepo.login)
    q = q.limit(num_jobs)

    print "query complete in {}sec".format(elapsed(query_start))

    start_time = time()
    index = 0
    for row in q.all():
        job = github_zip_queue.enqueue_call(
            func=add_github_dependency_lines,
            args=(row[0], row[1]),
            result_ttl=0  # number of seconds
        )
        job.meta["full_repo_name"] = row[0] + "/" + row[1]
        job.save()
        if index % 1000 == 0:
            print "added {} jobs to queue in {}sec".format(
                index,
                elapsed(start_time)
            )
        index += 1

    monitor_github_zip_queue(start_time, num_jobs)
    return True


def monitor_github_zip_queue(start_time, num_jobs):
    current_count = github_zip_queue.count
    time_per_job = 1
    while current_count:
        sleep(1)
        current_count = github_zip_queue.count
        done = num_jobs - current_count
        try:
            time_per_job = elapsed(start_time) / done
        except ZeroDivisionError:
            pass

        mins_left = int(current_count * time_per_job / 60)

        print "finished {done} jobs in {elapsed} min. {left} left (est {mins_left}min)".format(
            done=done,
            elapsed=int(elapsed(start_time) / 60),
            mins_left=mins_left,
            left=current_count
        )
    print "Done! {} jobs took {} seconds (avg {} secs/job)".format(
        num_jobs,
        elapsed(start_time),
        time_per_job
    )
    return True


def empty_github_zip_queue():
    print "emptying {} jobs on the github zip queue....".format(github_zip_queue.count)
    github_zip_queue.empty()
    print "done. there's {} jobs on the github zip queue now.".format(github_zip_queue.count)
