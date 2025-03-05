import argparse
import codecs
import os
import os.path as osp
import sys
from loguru import logger
import yaml
from app import MainWindow
from qtpy import QtCore
from qtpy import QtWidgets
from labelme import __appname__
from labelme import __version__
from labelme.config import get_config
from labelme.utils import newIcon


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', '-V', action='store_true', help='show version')
    parser.add_argument('--reset-config', action='store_true', help='reset qt config')
    parser.add_argument('--output', '-O', '-o')
    default_config_file = os.path.join(os.path.expanduser('~'), '.labelmerc')
    parser.add_argument('--config', dest='config', default=default_config_file)
    parser.add_argument(
        '--flags',
        help='comma separated list of flags OR file containing flags',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--labels',
        help='comma separated list of labels OR file containing labels',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--validatelabel',
        dest='validate_label',
        choices=['exact'],
        help='label validation types',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--keep-prev',
        action='store_true',
        help='keep annotation of previous frame',
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--epsilon',
        type=float,
        help='epsilon to find nearest vertex on canvas',
        default=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if args.version:
        print('{0} {1}'.format(__appname__, __version__))
        sys.exit(0)

    if hasattr(args, 'flags'):
        if os.path.isfile(args.flags):
            with codecs.open(args.flags, 'r', encoding='utf-8') as f:
                args.flags = [line.strip() for line in f if line.strip()]
        else:
            args.flags = [line for line in args.flags.split(',') if line]

    if hasattr(args, 'labels'):
        if os.path.isfile(args.labels):
            with codecs.open(args.labels, 'r', encoding='utf-8') as f:
                args.labels = [line.strip() for line in f if line.strip()]
        else:
            args.labels = [line for line in args.labels.split(',') if line]

    if hasattr(args, 'label_flags'):
        if os.path.isfile(args.label_flags):
            with codecs.open(args.label_flags, 'r', encoding='utf-8') as f:
                args.label_flags = yaml.safe_load(f)
        else:
            args.label_flags = yaml.safe_load(args.label_flags)

    config_from_args = args.__dict__
    config_from_args.pop('version')
    reset_config = config_from_args.pop('reset_config')
    output = config_from_args.pop('output')
    config_file_or_yaml = config_from_args.pop('config')
    config = get_config(config_file_or_yaml, config_from_args)

    if not config['labels'] and config['validate_label']:
        logger.error(
            '--labels must be specified with --validatelabel or '
            'validate_label: true in the config file '
            '(ex. ~/.labelmerc).'
        )
        sys.exit(1)

    output_file = None
    output_dir = None
    if output is not None:
        if output.endswith('.json'):
            output_file = output
        else:
            output_dir = output

    translator = QtCore.QTranslator()
    translator.load(
        QtCore.QLocale.system().name(),
        osp.dirname(osp.abspath(__file__)) + '/translate',
    )
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(__appname__)
    app.setWindowIcon(newIcon('icon'))
    app.installTranslator(translator)
    win = MainWindow(
        config=config,
        output_file=output_file,
        output_dir=output_dir,
    )

    if reset_config:
        logger.info('Resetting Qt config: %s' % win.settings.fileName())
        win.settings.clear()
        sys.exit(0)

    win.show()
    win.raise_()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
