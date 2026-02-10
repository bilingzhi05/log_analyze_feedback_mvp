import requests
from typing import Dict, Tuple

import re
from jira import JIRA

class MyJira:
    
    def __init__(self, jiraserver, username, password):
        self.mLogin_options = {"verify": False}
        self.mJiraServer = jiraserver
        self.mUserName = username
        self.mPassword = password
        self.build_jira()
        self.components_array = set()

    def build_jira(self):
        self.mJira = JIRA(self.mJiraServer, options=self.mLogin_options, basic_auth=(self.mUserName, self.mPassword))  # 创建jira连接

    def getBugAttachments(self, issue, patern, component_name):
        """
            :param issue_id: issue_id
            :return: 保存所有附件，如果没有附件则提示信息
        """
        fields = self.mJira.issue(id=issue.id, expand="summary").fields
        summary = fields.summary
        if len(summary) > 128:
            summary = summary[:128]
        print(f'summary:{"".join(summary)}')
        fields = self.mJira.issue(id=issue.id, expand="attachment").fields
        attachments = fields.attachment
        if len(attachments) != 0:
            need_dealwith = []
            no_need_dealwith =[]
            for i in range(len(attachments)):
                file_name = f"{attachments[i].filename}"
                if not re.match(patern, file_name):
                    no_need_dealwith.append(file_name)
                    continue
                need_dealwith.append(file_name)
                path = f"{''+issue.id+'_'+''.join(map(str,summary))+'_'+file_name}"
                path = path.replace(':','_').replace('/',"_")
                path = component_name+'/'+path
                with open(path, "wb") as f:
                    f.write(attachments[i].get())
            print(f'issuse id:{issue.id}, no need to deal with attachments of:{no_need_dealwith}\n')
            print(f'need to deal with attachments of:{need_dealwith}\n')
            print(f'--------->issue_id:{issue.id} attachment download finished!')
        else:
            print("没有附件")

    def getAllComponents(self):
        for project in self.mJira.projects():
            components = self.mJira.project_components(project)
            component_names = [component.name for i, component in enumerate(components)]
            for component_name in component_names:
                self.components_array.add(component_name)
    # if len(Component) != 0:
    #     self.components_array.add(set(Component))
    def getJiraStatus(self, key):
        """
            :param issue_id: issue_id
            :return: 返回jira状态
        """
        sql = f"key = {key}"
        issues = self.mJira.search_issues(sql)
        status_counts = {}
        for issue in issues:
            status_name = issue.fields.status.name
            # status_counts[status_name] = status_counts.get(status_name, 0) + 1
        return status_name

    def show_the_commponents(self):
        print(f'------->{self.components_array}')
    
    def search_issues(self, jql, maxResults=1000):
        """包装JIRA的search_issues方法"""
        return self.mJira.search_issues(jql, maxResults=maxResults)

    def getJiraLen(self, jql, maxResults=1000):
        issues = self.search_issues(jql, maxResults=maxResults)
        return len(issues)


def main():
    my_jira = MyJira("https://jira.amlogic.com", "lingzhi.bi", "Qwer!23456")
    status = my_jira.getJiraStatus(key='OTT-91431')
    print(status)
    sql = "assignee = \"lingzhi.bi\" AND labels = LN_TAG_2025_AI"
    lenth = my_jira.getJiraLen(sql)
    print(lenth)
    # issues = my_jira.search_issues(sql)
    # status_counts = {}
    # for issue in issues:
    #     status_name = issue.fields.status.name
    #     status_counts[status_name] = status_counts.get(status_name, 0) + 1
    # total = len(issues)
    # print(f"总数: {total}")
    # for status, count in status_counts.items():
    #     print(f"{status}: {count}")
if __name__ == "__main__":
    main()
