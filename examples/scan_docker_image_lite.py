'''
Created on March 25, 2021

@author: kumykov

Alternative version if Docker image layer by layer scan.

This program will download docker image and scan it into Blackduck server layer by layer
Each layer will be scanned as a separate scan with a signature scan.

Layers in the container images could be grouped into groups of contiguous layers.

I.e.
layers 1-5 - Group 1, layers 6-8 - Group 2, etc.

Each group will be scanned as a version within a project

Project naming will follow docker container image specification

      repository/image-name:version

Will create project named "repository/image-name" and will have "version" as a version prefix

Project versions corresponding to groups will be named 

        version_group_name
        
Scans will be named as

       repository/image-name_version_layer_1 
       repository/image-name_version_layer_2
        .........
        
Layers are numbered in chronological order

If a dockerfile or a base image spec is available, grouping could be done based on the 
information gathered from those sources.

layers that are present in the base image will be grouped as *_base_*
layers that are not present in the base image will be grouped as *_addon_*



Usage:

scan_docker_image_slim.py [-h] imagespec  [--grouping=group_end:group_name,group_end:group_name] | [--dockerfile=Dockerfile | --base-image=baseimagespec]

positional arguments:
  imagespec             Container image tag, e.g. repository/imagename:version

optional arguments:
  -h, --help            show this help message and exit
  --grouping GROUPING   Group layers into user defined provect versions (can't be used with --base-image)
  --base-image BASE_IMAGE
                        Use base image spec to determine base image/layers (can't be used with --grouping or
                        --dockerfile)
  --dockerfile DOCKERFILE
                        Use Dockerfile to determine base image/layers (can't be used with --grouping or ---base-image)
  --project-name        Specify project name (default is container image spec)
  --project-verson      Specify project version (default is container image tag/version)
  --detect-options DETECT_OPTIONS
                        Extra detect options to be passed directlyto the detect


Using --detect-options

It is possible to pass detect options directly to detect command.
For example one wants to specify cloning options directly

python3 scan_docker_image_lite.py <imagespec> --detect-options='--detect.clone.project.version.name=version --detect.project.clone.categories=COMPONENT_DATA,VULN_DATA'

There is not validation of extra parameters passed, use with care.
'''

from blackduck.HubRestApi import HubInstance
from pprint import pprint
from sys import argv
import json
import os
import requests
import shutil
import subprocess
import sys
from argparse import ArgumentParser
import argparse

#hub = HubInstance()

'''
quick and dirty wrapper to process some docker functionality
'''
class DockerWrapper():
   
    def __init__(self, workdir, scratch = True):
        self.workdir = workdir
        self.imagedir = self.workdir + "/container"
        self.imagefile = self.workdir + "/image.tar"
        if scratch:
            self.initdir()
        self.docker_path = self.locate_docker()
        
    def initdir(self):
        if os.path.exists(self.workdir):
            if os.path.isdir(self.workdir):
                shutil.rmtree(self.workdir)
            else:
                os.remove(self.workdir)
        os.makedirs(self.workdir, 0o755, True)
        os.makedirs(self.workdir + "/container", 0o755, True)

        
    def locate_docker(self):
        os.environ['PATH'] += os.pathsep + '/usr/local/bin'
        args = []
        args.append('/usr/bin/which')
        args.append('docker')
        proc = subprocess.Popen(['which','docker'], stdout=subprocess.PIPE)
        out, err = proc.communicate()
        lines = out.decode().split('\n')
        print(lines)
        if 'docker' in lines[0]:
            return lines[0]
        else:
            raise Exception('Can not find docker executable in PATH.')
        
    def pull_container_image(self, image_name):
        args = []
        args.append(self.docker_path)
        args.append('pull')
        args.append(image_name)
        return subprocess.run(args)
        
    def save_container_image(self, image_name):
        args = []
        args.append(self.docker_path)
        args.append('save')
        args.append('-o')
        args.append(self.imagefile)
        args.append(image_name)
        return subprocess.run(args)
    
    def unravel_container(self):
        args = []
        args.append('tar')
        args.append('xvf')
        args.append(self.imagefile)
        args.append('-C')
        args.append(self.imagedir)
        return subprocess.run(args)
    
    def read_manifest(self):
        filename = self.imagedir + "/manifest.json"
        with open(filename) as fp:
            data = json.load(fp)
        return data
        
    def read_config(self):
        manifest = self.read_manifest()
        configFile = self.imagedir + "/" + manifest[0]['Config']
        with open(configFile) as fp:
            data = json.load(fp)
        return data
 
class Detector():
    def __init__(self, hub):
        # self.detecturl = 'https://blackducksoftware.github.io/hub-detect/hub-detect.sh'
        # self.detecturl = 'https://detect.synopsys.com/detect.sh'
        self.detecturl = 'https://detect.synopsys.com/detect7.sh'
        self.baseurl = hub.config['baseurl']
        self.filename = '/tmp/hub-detect.sh'
        self.token=hub.config['api_token']
        self.baseurl=hub.config['baseurl']
        self.download_detect()
        
    def download_detect(self):
        with open(self.filename, "wb") as file:
            response = requests.get(self.detecturl)
            file.write(response.content)

    def detect_run(self, options=['--help']):
        cmd = ['bash']
        cmd.append(self.filename)
        cmd.append('--blackduck.url=%s' % self.baseurl)
        cmd.append('--blackduck.api.token=' + self.token)
        cmd.append('--blackduck.trust.cert=true')
        cmd.extend(options)
        subprocess.run(cmd)

class ContainerImageScanner():
    
    def __init__(
        self, hub, container_image_name, workdir='/tmp/workdir', 
        grouping=None, base_image=None, dockerfile=None, detect_options=None):
        self.hub = hub
        self.hub_detect = Detector(hub)
        self.docker = DockerWrapper(workdir)
        self.container_image_name = container_image_name
        cindex = container_image_name.rfind(':')
        if cindex == -1:
            self.image_name = container_image_name
            self.image_version = 'latest'
        else:
            self.image_name = container_image_name[:cindex]
            self.image_version = container_image_name[cindex+1:]
        self.grouping = grouping
        self.base_image = base_image
        self.dockerfile = dockerfile
        self.base_layers = None
        self.project_name = self.image_name
        self.project_version = self.image_version
        self.extra_options = []
        if detect_options:
            self.extra_options = detect_options.split(" ")
        print ("<--{}-->".format(self.grouping))
              
    def prepare_container_image(self):
        self.docker.initdir()
        self.docker.pull_container_image(self.container_image_name)
        self.docker.save_container_image(self.container_image_name)
        self.docker.unravel_container()

    def process_container_image_by_user_defined_groups(self):
        self.manifest = self.docker.read_manifest()
        print(self.manifest)
        self.config = self.docker.read_config()
        print (json.dumps(self.config, indent=4))
        
        if self.grouping:
            self.groups = dict(x.split(":") for x in self.grouping.split(","))

        self.layers = []
        num = 1
        offset = 0
        for i in self.manifest[0]['Layers']:
            layer = {}
            if self.grouping:
                intlist = [int(x) for x in sorted(self.groups.keys())]
                intlist.sort()
                key_number = len(self.groups) - len([i for i in intlist if i >= num])
                if key_number >= len(self.groups):
                    layer['group_name'] = "undefined"
                else:
                    layer['group_name'] = self.groups.get(str(intlist[key_number]))
                layer['project_version'] = "{}_{}".format(self.project_version,layer['group_name'])
                layer['name'] = "{}_{}_{}_layer_{}".format(self.project_name,self.project_version,layer['group_name'],str(num))
            else:
                layer['project_version'] = self.project_version
                layer['name'] = self.project_name + "_" + self.project_version + "_layer_" + str(num)
            layer['project_name'] = self.project_name
            layer['path'] = i
            while self.config['history'][num + offset -1].get('empty_layer', False):
                offset = offset + 1
            layer['command'] = self.config['history'][num + offset - 1]
            layer['shaid'] = self.config['rootfs']['diff_ids'][num - 1]
            self.layers.append(layer)
            num = num + 1
        print (json.dumps(self.layers, indent=4))

    def process_container_image_by_base_image_info(self):
        self.manifest = self.docker.read_manifest()
        print(self.manifest)
        self.config = self.docker.read_config()
        print (json.dumps(self.config, indent=4))

        self.layers = []
        num = 1
        offset = 0
        for i in self.manifest[0]['Layers']:
            layer = {}
            layer['project_name'] = self.project_name
            layer['path'] = i
            while self.config['history'][num + offset -1].get('empty_layer', False):
                offset = offset + 1
            layer['command'] = self.config['history'][num + offset - 1]
            layer['shaid'] = self.config['rootfs']['diff_ids'][num - 1]

            if self.base_layers:
                pass
                if layer['shaid'] in self.base_layers:
                    layer['project_version'] = "{}_{}".format(self.project_version,'base')
                    layer['name'] = "{}_{}_{}_layer_{}".format(self.project_name,self.project_version,'base',str(num))
                else:
                    layer['project_version'] = "{}_{}".format(self.project_version,'addon')
                    layer['name'] = "{}_{}_{}_layer_{}".format(self.project_name,self.project_version,'addon',str(num))
            else:
                layer['project_version'] = self.project_version
                layer['name'] = self.project_name + "_" + self.project_version + "_layer_" + str(num)
            self.layers.append(layer)
            num = num + 1
        print (json.dumps(self.layers, indent=4))

    def process_container_image(self):
        if self.grouping:
            self.process_container_image_by_user_defined_groups()
        else:
            self.process_container_image_by_base_image_info()

    def submit_layer_scans(self):
        for layer in self.layers:
            options = []
            options.append('--detect.project.name={}'.format(layer['project_name']))
            options.append('--detect.project.version.name="{}"'.format(layer['project_version']))
            # options.append('--detect.blackduck.signature.scanner.disabled=false')
            options.append('--detect.code.location.name={}_{}_code_{}'.format(layer['name'],self.image_version,layer['path']))
            options.append('--detect.source.path={}/{}'.format(self.docker.imagedir, layer['path'].split('/')[0]))
            options.extend(self.extra_options)
            self.hub_detect.detect_run(options)

    def get_base_layers(self):
        if (not self.dockerfile)and (not self.base_image):
            raise Exception ("No dockerfile or base image specified")
        imagelist = []
        
        if self.dockerfile:
            from pathlib import Path
            dfile = Path(self.dockerfile)
            if not dfile.exists():
                raise Exception ("Dockerfile {} does not exist",format(self.dockerfile))
            if not dfile.is_file():
                raise Exception ("{} is not a file".format(self.dockerfile))
            with open(dfile) as f:
                for line in f:
                    if 'FROM' in line.upper():
                        a = line.split()
                        if a[0].upper() == 'FROM':
                            imagelist.append(a[1])                    
        if self.base_image:
            imagelist.append(self.base_image)
        
        print (imagelist)
        base_layers = []
        for image in imagelist:
            self.docker.initdir()
            self.docker.pull_container_image(image)
            self.docker.save_container_image(image)
            self.docker.unravel_container()
            manifest = self.docker.read_manifest()
            print(manifest)
            config = self.docker.read_config()
            print(config)
            base_layers.extend(config['rootfs']['diff_ids'])
        return base_layers  
    

def scan_container_image(
    imagespec, grouping=None, base_image=None, dockerfile=None, 
    project_name=None, project_version=None, detect_options=None):
    
    hub = HubInstance()
    scanner = ContainerImageScanner(
        hub, imagespec, grouping=grouping, base_image=base_image, 
        dockerfile=dockerfile, detect_options=detect_options)
    if project_name:
        scanner.project_name = project_name
    if project_version:
        scanner.project_version = project_version
    if not grouping:
        if not base_image and not dockerfile:
            scanner.grouping = '1024:everything'
        else:
            scanner.base_layers = scanner.get_base_layers()
    scanner.prepare_container_image()
    scanner.process_container_image()
    scanner.submit_layer_scans()

def main(argv=None):
    
    if argv is None:
        argv = sys.argv
    else:
        argv.extend(sys.argv)
        
    parser = ArgumentParser()
    parser.add_argument('imagespec', help="Container image tag, e.g.  repository/imagename:version")
    parser.add_argument('--grouping',default=None, type=str, help="Group layers into user defined provect versions (can't be used with --base-image)")
    parser.add_argument('--base-image',default=None, type=str, help="Use base image spec to determine base image/layers (can't be used with --grouping or --dockerfile)")
    parser.add_argument('--dockerfile',default=None, type=str, help="Use Dockerfile to determine base image/layers (can't be used with --grouping or ---base-image)")
    parser.add_argument('--project-name',default=None, type=str, help="Specify project name (default is container image spec)")
    parser.add_argument('--project-version',default=None, type=str, help="Specify project version (default is container image tag/version)")
    parser.add_argument('--detect-options',default=None, type=str, help="Extra detect options to be passed directlyto the detect")
    
    args = parser.parse_args()
    
    print (args);

    if not args.imagespec:
        parser.print_help(sys.stdout)
        sys.exit(1)

    if args.dockerfile and args.base_image:
        parser.print_help(sys.stdout)
        sys.exit(1)
    
    if args.grouping and (args.dockerfile and args.base_image):
        parser.print_help(sys.stdout)
        sys.exit(1)

    scan_container_image(
        args.imagespec, 
        args.grouping, 
        args.base_image, 
        args.dockerfile, 
        args.project_name, 
        args.project_version,
        args.detect_options)
        
    
if __name__ == "__main__":
    sys.exit(main())
    
