import logging
import os
import champ
import shutil

log = logging.getLogger(__name__)


def main(clargs):
    current_directory = os.getcwd()
    log.debug("Current directory: {current}".format(current=current_directory))
    base = os.path.join(os.path.abspath(os.path.join(os.path.dirname(champ.__file__), '..')), 'notebooks')
    log.debug("Base notebook directory: {base}".format(base=base))
    notebooks = os.listdir(base)
    log.debug("Notebooks: {notebooks}".format(notebooks=" ".join(notebooks)))
    for notebook in notebooks:
        notebook_path = os.path.join(base, notebook)
        destination = os.path.join(current_directory, notebook)
        if os.path.exists(destination):
            print("{notebook} already exists! SKIPPING!".format(notebook=notebook))
            continue
        shutil.copy(notebook_path, destination)
        print("Created {notebook}".format(notebook=notebook))
