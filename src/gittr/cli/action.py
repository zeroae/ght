#!/usr/bin/env python3

import os
import sys
from itertools import zip_longest
from platform import release

import yaml
from jinja2 import Environment, FileSystemLoader, Template, TemplateNotFound
from git import Repo, GitConfigParser, Remote, Blob, Object


class RestrictedFileSystemLoader(FileSystemLoader):
    def get_source(self, environment, template):
        self._ensure_not_unsafe_github(template)
        self._ensure_not_git(template)

        return super().get_source(environment, template)

    def list_templates(self):
        def only_safe(template):
            try:
                self._ensure_not_git(template)
                self._ensure_not_unsafe_github(template)
                return True
            except TemplateNotFound:
                return False

        return filter(only_safe, super().list_templates())

    @staticmethod
    def _ensure_not_unsafe_github(template):
        if template.startswith(".github/") and not (template.endswith(".ght") or template.endswith(".j2")):
            raise TemplateNotFound(f"Templates under the .github/ folder must end in .ght or j2: {template}")

    @staticmethod
    def _ensure_not_git(template):
        if template.startswith(".git/"):
            raise TemplateNotFound(f"The .git folder is not a valid path for templates: {template}")


class GHT(object):
    repo: Repo
    env: Environment
    config: dict
    template_url: str

    __slots__ = ["repo", "env", "config", "template_url"]

    def __init__(self, repo_path, template_url):
        self.repo = Repo(path=repo_path)
        self.template_url = template_url

        self.env = Environment(
            loader=RestrictedFileSystemLoader(self.repo.working_tree_dir),
            extensions=[
                "jinja2.ext.do",
                "jinja2.ext.loopcontrols",
                "jinja2.ext.with_",
                "jinja2_time.TimeExtension"
            ]
        )

        self.load_config()
        self.fetch_template()
        self.configure_author()

    def load_config(self):
        if not os.path.exists(ght_conf := os.path.join(self.repo.working_tree_dir, ".github", "ght.yaml")):
            raise ValueError(f"{self.repo.working_tree_dir} is an invalid GHT repository. No .github/ght.yaml file found.")
        with open(ght_conf, "r") as f:
            self.config = yaml.load(f, Loader=yaml.SafeLoader)

    def configure_author(self):
        """
        git config --local user.email ""
        git config --local user.name ""
        """
        cw: GitConfigParser
        with self.repo.config_writer() as cw:
            cw.set_value("user", "email", "psodre@gmail.com")
            cw.set_value("user", "name", "Patrick Sodré")
        release()

    def prepare_tree_for_rendering(self):
        """
        git rm -rf .
        git checkout ght/template -- .
        git checkout HEAD -- .github/ght.yaml
        """
        self.remove_all()

        self.repo.git.checkout("ght/template", "--", ".")

        self.repo.git.checkout("HEAD", "--", ".github/ght.yaml")

    def fetch_template(self):
        ght_url, refspec = self.template_url.split("@")
        self.repo.git.fetch(ght_url, f"{refspec}:ght/template")

    def remove_all(self):
        """
        Does the equivalent of `git rm -rf .`
        """
        all_blobs = [o.path for o in self.repo.tree().traverse(
            predicate=lambda i, _: i.type == "blob",
            branch_first=False)]
        for path in all_blobs:
            fs_path = os.path.join(self.repo.working_tree_dir, path)
            os.remove(fs_path)
        self.repo.index.remove(all_blobs)
        all_trees = [o.path for o in self.repo.tree().traverse(
            predicate=lambda i, _: i.type == "tree",
            branch_first=False
        )]
        all_trees.reverse()
        for path in all_trees:
            os.rmdir(os.path.join(self.repo.working_tree_dir, path))
        self.repo.index.update()

    def render_ght_conf(self):
        """
        Render the .github/ght.yaml file
        """
        ght_conf_path = os.path.join(self.repo.working_tree_dir, ".github", "ght.yaml")
        with open(ght_conf_path) as f:
            curr_ght_yaml = f.read().splitlines()
        next_ght_yaml = curr_ght_yaml

        converged, index = False, -1
        while not converged:
            curr_ght_yaml = next_ght_yaml[:index+1] + curr_ght_yaml[index+1:]
            config = yaml.safe_load("\n".join(curr_ght_yaml))
            next_ght_yaml = [self.env.from_string(line).render(config)
                             for line in curr_ght_yaml]
            converged, index = iterable_converged(curr_ght_yaml, next_ght_yaml)

        with open(ght_conf_path, "w") as f:
            f.write("\n".join(curr_ght_yaml))
        self.repo.index.add(".github/ght.yaml")

    def render_tree(self):
        self.prepare_tree_for_rendering()
        self.render_ght_conf()
        self.load_config()
        self.render_tree_content()
        self.repo.index.commit(f"[ght]: rendered {self.template_url} content")
        self.render_tree_structure()
        self.repo.index.commit(f"[ght]: rendered {self.template_url} structure")

    def render_tree_structure(self):
        """
        Renders the Tree structure in git, by applying `render_ght_obj_name` to each object name.
        """
        objs_to_rename = [(o.path, os.path.join(os.path.dirname(o.path), new_name))
                          for o in self.repo.tree().traverse(branch_first=False)
                          if o.name != (new_name := self.render_ght_obj_name(o.name))]
        objs_to_rename.reverse()

        for old_new in objs_to_rename:
            self.repo.index.move(old_new)
            self.repo.index.update()

    def render_ght_obj_name(self, name):
        if name.endswith(".ght"):
            rv = name[:-4]
        else:
            rv = name
        return self.env.from_string(rv).render(self.config)

    def render_tree_content(self):
        """
        Render all tree content
        """
        paths_to_render = [o.path for _, o in
                           self.repo.index.iter_blobs()
                           if not o.path.startswith(".github/") or o.path.endswith(".ght")]

        for path in paths_to_render:
            template: Template = self.env.get_template(path)
            rendered = template.render(self.config)
            with open(os.path.join(self.repo.working_tree_dir, path), "w") as f:
                f.write(rendered)
            self.repo.index.add(path)

    @classmethod
    def init(cls, path, template_url, config: dict = None):
        repo = Repo.init(path)
        git_url, refspec = template_url.split("@")
        repo.git.fetch(git_url, f"{refspec}:ght/master")
        repo.git.checkout("ght/master")

        if config:
            os.makedirs(github_dir := os.path.join(path, ".github"), exist_ok=True)
            with open(os.path.join(github_dir, 'ght.yaml'), 'w') as f:
                yaml.dump(config, f)
            repo.index.add('.github/ght.yaml')

        repo.index.commit("[ght]: Initial Commit")

        return cls(repo_path=path, template_url=template_url)


def iterable_converged(left, right):
    """
    Returns True, None if the two iterables generate identical, False, index otherwise.
    The index indicates the first position where the iterables differ
    """
    for i, (l, r) in enumerate(zip_longest(left, right)):
        if l != r:
            return False, i
    return True, None


def commit_and_push():
    """
    git commit -m "<something meaningful>"
    git push --set-upstream origin HEAD:${{ github.head_ref }} --force
    """


def go():
    template_url = sys.argv[1]
    # sender = sys.argv[2]

    # Setup the GHT Repository
    ght = GHT(repo_path=".", template_url=template_url)
    ght.render_tree()

    #os.system('echo ::set-output name=reply::Hello %s!' % ght_url)
    return 0


if __name__ == "__main__":
    sys.exit(go())
