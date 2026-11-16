"""Checks; --all starts and destroys a private Unix-socket PostgreSQL instance."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--all', action='store_true', help='require live PostgreSQL integration checks')
    arguments = parser.parse_args()
    if not arguments.all:
        return subprocess.call([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], cwd=ROOT)
    binaries = sorted(Path('/usr/lib/postgresql').glob('*/bin/pg_ctl'))
    if not binaries:
        raise SystemExit('PostgreSQL server binaries missing; install postgresql or run core checks without --all')
    bin_dir = binaries[-1].parent
    with tempfile.TemporaryDirectory(prefix='fde-pg-') as temporary:
        data = Path(temporary) / 'data'
        sock = Path(temporary) / 'sock'
        sock.mkdir(mode=0o700)
        subprocess.run([str(bin_dir/'initdb'), '-D', str(data), '-A', 'trust', '--no-locale', '-E', 'UTF8'],
                       check=True, capture_output=True)
        started = False
        try:
            subprocess.run([str(bin_dir/'pg_ctl'), '-D', str(data), '-l', str(Path(temporary)/'postgres.log'),
                            '-o', f"-h '' -k {sock} -p 55432", '-w', 'start'],
                           check=True, capture_output=True)
            started = True
            import psycopg
            dsn = f'host={sock} port=55432 dbname=postgres'
            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute((ROOT/'sql/schema.sql').read_text())
                connection.execute('CREATE ROLE fde_test_reader LOGIN INHERIT NOSUPERUSER NOBYPASSRLS IN ROLE fde_reader')
                connection.execute('CREATE ROLE fde_test_ingest LOGIN INHERIT NOSUPERUSER NOBYPASSRLS IN ROLE fde_ingest')
            environment = {**os.environ, 'FDE_TEST_DSN': dsn}
            return subprocess.call([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                                   cwd=ROOT, env=environment)
        finally:
            if started:
                subprocess.run([str(bin_dir/'pg_ctl'), '-D', str(data), '-m', 'fast', '-w', 'stop'],
                               check=True, capture_output=True)


if __name__ == '__main__':
    raise SystemExit(main())
